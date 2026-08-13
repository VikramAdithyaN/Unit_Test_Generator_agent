from __future__ import annotations

import json
import os
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent import git_utils
from agent.prompts import (
    CLASSIFIER_SYSTEM_V1,
    GAP_ANALYZER_SYSTEM_V1,
    GENERATOR_SYSTEM_V1,
    PLANNER_SYSTEM_V1,
    REVISION_SYSTEM_V1,
)
from agent.sandbox import run_pytest, run_repo_test_suite
from agent.state import (
    AgentState,
    ChangedFile,
    CoverageGap,
    FailureClassification,
    FileWorkItem,
)
from agent.tools import (
    detect_language,
    find_existing_test,
    test_path_for,
    tier_for,
)


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.environ.get("MODEL_NAME", "gpt-4o-mini"),
        temperature=0.1,
    )


def _strip_fences(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def ingest_pr(state: AgentState) -> dict:
    """Read base + head content for each changed file via git."""
    repo = state["repo_path"]
    pr = state.get("pr_context", {})
    base_ref = pr.get("base_ref")
    head_ref = pr.get("head_ref", "HEAD")

    seed_items = state.get("work_items", [])
    work_items: list[FileWorkItem] = []
    errors: list[str] = []

    for item in seed_items:
        rel = item["file"]["path"]
        language = detect_language(rel)
        tier = tier_for(language)

        base_content = None
        if base_ref:
            base_content = git_utils.read_file_at_ref(repo, base_ref, rel)

        head_content = git_utils.read_file_at_ref(repo, head_ref, rel)
        if head_content is None:
            try:
                from pathlib import Path
                head_content = (Path(repo) / rel).read_text()
            except FileNotFoundError:
                errors.append(f"skip {rel}: not found at head")
                continue

        existing = find_existing_test(repo, rel, language)

        cf: ChangedFile = {
            "path": rel,
            "language": language,
            "tier": tier,
            "base_content": base_content,
            "head_content": head_content,
            "existing_tests": existing,
        }
        work_items.append(
            FileWorkItem(
                file=cf,
                attempts=0,
                status="pending",
                test_path=test_path_for(rel, language),
                coverage_gaps=[],
            )
        )

    return {"work_items": work_items, "current_index": 0, "errors": errors}


def run_existing_suite(state: AgentState) -> dict:
    """Run the target repo's own test suite against HEAD."""
    result = run_repo_test_suite(state["repo_path"])
    return {"existing_suite": result}


_GAP_LINE_RE = re.compile(r"^-\s*(.+?):\s*(.+)$")


def compute_coverage_gaps(state: AgentState) -> dict:
    """LLM: for each file, list coverage gaps the diff touches."""
    items = state["work_items"]
    pr = state.get("pr_context", {})
    llm = _llm()

    for i, item in enumerate(items):
        file = item["file"]
        if not file["base_content"] and not file["head_content"]:
            continue

        user = (
            f"Language: {file['language']}\n"
            f"Path: {file['path']}\n\n"
            f"PR title: {pr.get('title', '(none)')}\n"
            f"PR body: {pr.get('body', '(none)')[:2000]}\n\n"
            f"BASE version:\n```\n{(file['base_content'] or '(file did not exist)')[:6000]}\n```\n\n"
            f"HEAD version:\n```\n{(file['head_content'] or '(file deleted)')[:6000]}\n```\n\n"
            f"Existing test file:\n```\n{(file['existing_tests'] or '(none found)')[:6000]}\n```\n"
        )
        resp = llm.invoke([
            SystemMessage(content=GAP_ANALYZER_SYSTEM_V1),
            HumanMessage(content=user),
        ])
        text = resp.content.strip()

        gaps: list[CoverageGap] = []
        if text.upper() != "NONE":
            for line in text.splitlines():
                m = _GAP_LINE_RE.match(line.strip())
                if m:
                    gaps.append(CoverageGap(what=m.group(1).strip(), why=m.group(2).strip()))

        items[i] = {
            **item,
            "coverage_gaps": gaps,
            "status": "pending" if gaps else "no_gaps",
        }

    return {"work_items": items}


def _next_actionable_index(items: list[FileWorkItem], start: int) -> int:
    """Advance past files with no gaps."""
    i = start
    while i < len(items) and items[i].get("status") == "no_gaps":
        i += 1
    return i


def plan_tests(state: AgentState) -> dict:
    """LLM: produce a bullet plan for the current file's gaps, using BASE code."""
    items = state["work_items"]
    idx = _next_actionable_index(items, state["current_index"])
    if idx >= len(items):
        return {"current_index": idx}

    item = items[idx]
    file = item["file"]
    pr = state.get("pr_context", {})

    gaps_str = "\n".join(f"- {g['what']}: {g['why']}" for g in item.get("coverage_gaps", []))
    base = file["base_content"] or file["head_content"] or ""

    user = (
        f"Language: {file['language']}\n"
        f"Path: {file['path']}\n"
        f"PR title: {pr.get('title', '(none)')}\n"
        f"PR body: {pr.get('body', '(none)')[:1500]}\n\n"
        f"Coverage gaps to address:\n{gaps_str}\n\n"
        f"BASE source (source of truth):\n```\n{base[:8000]}\n```\n"
    )
    if file["existing_tests"]:
        user += f"\nExisting tests (style reference only):\n```\n{file['existing_tests'][:3000]}\n```\n"

    resp = _llm().invoke([
        SystemMessage(content=PLANNER_SYSTEM_V1),
        HumanMessage(content=user),
    ])
    items[idx] = {**item, "plan": resp.content}
    return {"work_items": items, "current_index": idx}


def generate_tests(state: AgentState) -> dict:
    """LLM: write regression tests from BASE code. Uses revision prompt on retry."""
    items = state["work_items"]
    idx = state["current_index"]
    item = items[idx]
    file = item["file"]
    attempt = item.get("attempts", 0)
    base = file["base_content"] or file["head_content"] or ""

    if attempt == 0:
        system = GENERATOR_SYSTEM_V1
        user = (
            f"Language: {file['language']}\n"
            f"Source path: {file['path']}\n"
            f"Test path: {item['test_path']}\n\n"
            f"Plan:\n{item.get('plan', '')}\n\n"
            f"BASE source file:\n```\n{base[:8000]}\n```\n"
        )
        if file["existing_tests"]:
            user += f"\nExisting test style reference:\n```\n{file['existing_tests'][:3000]}\n```\n"
    else:
        system = REVISION_SYSTEM_V1
        prev = item.get("test_code", "")
        exec_ = item.get("execution", {})
        user = (
            f"Attempt #{attempt + 1}.\n\n"
            f"BASE source file ({file['path']}):\n```\n{base[:8000]}\n```\n\n"
            f"Previous test file:\n```\n{prev}\n```\n\n"
            f"Runner stderr:\n```\n{exec_.get('stderr', '')[-3000:]}\n```\n\n"
            f"Runner stdout:\n```\n{exec_.get('stdout', '')[-3000:]}\n```\n"
        )

    resp = _llm().invoke([SystemMessage(content=system), HumanMessage(content=user)])
    test_code = _strip_fences(resp.content)

    items[idx] = {
        **item,
        "test_code": test_code,
        "attempts": attempt + 1,
        "status": "generated",
    }
    return {"work_items": items}


def execute_tests(state: AgentState) -> dict:
    """Run the generated test file against HEAD (the copied working tree)."""
    items = state["work_items"]
    idx = state["current_index"]
    item = items[idx]
    file = item["file"]

    if not state.get("validate", True) or file["tier"] != "tier1":
        items[idx] = {**item, "status": "unverified"}
        return {"work_items": items}

    if file["language"] != "python":
        items[idx] = {**item, "status": "unverified"}
        return {"work_items": items}

    result = run_pytest(state["repo_path"], item["test_path"], item["test_code"])
    new_status = "validated" if result["passed"] else "failed"
    items[idx] = {**item, "execution": result, "status": new_status}
    return {"work_items": items}


def classify_failure(state: AgentState) -> dict:
    """LLM: decide if the failure is intentional (matches PR intent) or suspicious."""
    items = state["work_items"]
    idx = state["current_index"]
    item = items[idx]
    file = item["file"]
    pr = state.get("pr_context", {})
    exec_ = item.get("execution", {})

    user = (
        f"PR title: {pr.get('title', '(none)')}\n"
        f"PR body: {pr.get('body', '(none)')[:2000]}\n\n"
        f"BASE source:\n```\n{(file['base_content'] or '')[:6000]}\n```\n\n"
        f"HEAD source:\n```\n{(file['head_content'] or '')[:6000]}\n```\n\n"
        f"Failing test file:\n```\n{item.get('test_code', '')[:4000]}\n```\n\n"
        f"Runner stderr:\n```\n{exec_.get('stderr', '')[-2000:]}\n```\n\n"
        f"Runner stdout:\n```\n{exec_.get('stdout', '')[-2000:]}\n```\n"
    )
    resp = _llm().invoke([
        SystemMessage(content=CLASSIFIER_SYSTEM_V1),
        HumanMessage(content=user),
    ])

    verdict = "unknown"
    reasoning = resp.content.strip()
    try:
        parsed = json.loads(_strip_fences(resp.content))
        verdict = parsed.get("verdict", "unknown")
        reasoning = parsed.get("reasoning", reasoning)
    except json.JSONDecodeError:
        pass

    classification: FailureClassification = {
        "verdict": verdict if verdict in ("intentional", "suspicious", "unknown") else "unknown",
        "reasoning": reasoning,
    }
    items[idx] = {
        **item,
        "failure_classification": classification,
        "status": "unverified",
    }
    return {"work_items": items}


def evaluate(state: AgentState) -> str:
    """Router: retry, classify (retries exhausted), advance to next file, or finish."""
    items = state["work_items"]
    idx = state["current_index"]
    item = items[idx]
    max_attempts = state.get("max_attempts", 3)
    status = item.get("status")
    attempts = item.get("attempts", 0)

    if status == "failed" and attempts < max_attempts:
        return "retry"
    if status == "failed":
        return "classify"

    next_idx = _next_actionable_index(items, idx + 1)
    if next_idx < len(items):
        return "next"
    return "done"


def advance_next(state: AgentState) -> dict:
    items = state["work_items"]
    next_idx = _next_actionable_index(items, state["current_index"] + 1)
    return {"current_index": next_idx}


def deliver(state: AgentState) -> dict:
    """CLI-mode: write generated tests to <repo>/generated_tests/.
    SCM-mode: no-op; the caller pipeline pulls files off state and pushes them."""
    mode = state.get("deliver_mode", "cli")
    if mode != "cli":
        return {}

    from pathlib import Path

    out_root = Path(state["repo_path"]) / "generated_tests"
    out_root.mkdir(parents=True, exist_ok=True)

    for item in state["work_items"]:
        code = item.get("test_code")
        if not code:
            continue
        dest = out_root / item["test_path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code)

    return {}
