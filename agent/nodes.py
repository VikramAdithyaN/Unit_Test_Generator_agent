from __future__ import annotations

import os
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.prompts import (
    GENERATOR_SYSTEM_V1,
    PLANNER_SYSTEM_V1,
    REVISION_SYSTEM_V1,
)
from agent.sandbox import run_pytest
from agent.state import AgentState, ChangedFile, FileWorkItem
from agent.tools import (
    detect_language,
    find_existing_test,
    read_source,
    test_path_for,
    tier_for,
)


def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.environ.get("MODEL_NAME", "gpt-4o-mini"),
        temperature=0.1,
    )


def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the model included them despite instructions."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def ingest_pr(state: AgentState) -> dict:
    """Read each changed source file from disk and build work items."""
    repo = state["repo_path"]
    files_from_input = state.get("work_items", [])
    work_items: list[FileWorkItem] = []
    errors: list[str] = []

    for item in files_from_input:
        rel = item["file"]["path"]
        try:
            content = read_source(repo, rel)
        except FileNotFoundError as exc:
            errors.append(f"skip {rel}: {exc}")
            continue

        language = detect_language(rel)
        tier = tier_for(language)
        existing = find_existing_test(repo, rel, language)

        cf: ChangedFile = {
            "path": rel,
            "language": language,
            "tier": tier,
            "content": content,
            "existing_tests": existing,
        }
        work_items.append(
            FileWorkItem(
                file=cf,
                attempts=0,
                status="pending",
                test_path=test_path_for(rel, language),
            )
        )

    return {
        "work_items": work_items,
        "current_index": 0,
        "errors": errors,
    }


def plan_tests(state: AgentState) -> dict:
    """LLM: produce a bullet-point test plan for the current file."""
    idx = state["current_index"]
    items = state["work_items"]
    item = items[idx]
    file = item["file"]

    prompt = (
        f"Language: {file['language']}\n"
        f"Path: {file['path']}\n\n"
        f"Source:\n```\n{file['content']}\n```\n"
    )
    if file["existing_tests"]:
        prompt += (
            f"\nExisting tests nearby (for style reference only):\n"
            f"```\n{file['existing_tests'][:4000]}\n```\n"
        )

    resp = _llm().invoke([
        SystemMessage(content=PLANNER_SYSTEM_V1),
        HumanMessage(content=prompt),
    ])

    items[idx] = {**item, "plan": resp.content}
    return {"work_items": items}


def generate_tests(state: AgentState) -> dict:
    """LLM: generate the test file. Uses revision prompt if this is a retry."""
    idx = state["current_index"]
    items = state["work_items"]
    item = items[idx]
    file = item["file"]
    attempt = item.get("attempts", 0)

    if attempt == 0:
        system = GENERATOR_SYSTEM_V1
        user = (
            f"Language: {file['language']}\n"
            f"Source path: {file['path']}\n"
            f"Test path: {item['test_path']}\n\n"
            f"Plan:\n{item.get('plan', '')}\n\n"
            f"Source file:\n```\n{file['content']}\n```\n"
        )
        if file["existing_tests"]:
            user += f"\nExisting test style reference:\n```\n{file['existing_tests'][:4000]}\n```\n"
    else:
        system = REVISION_SYSTEM_V1
        prev = item.get("test_code", "")
        err = item.get("execution", {}).get("stderr", "")
        out = item.get("execution", {}).get("stdout", "")
        user = (
            f"Attempt #{attempt + 1}. The previous test file failed.\n\n"
            f"Source file ({file['path']}):\n```\n{file['content']}\n```\n\n"
            f"Previous test file:\n```\n{prev}\n```\n\n"
            f"Runner stderr:\n```\n{err[-3000:]}\n```\n\n"
            f"Runner stdout:\n```\n{out[-3000:]}\n```\n"
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
    """Run the generated test file. Skips for tier2 or when validation is off."""
    idx = state["current_index"]
    items = state["work_items"]
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


def evaluate(state: AgentState) -> str:
    """Routing node: decide whether to retry, move to next file, or finish."""
    idx = state["current_index"]
    items = state["work_items"]
    item = items[idx]
    max_attempts = state.get("max_attempts", 3)
    status = item.get("status")
    attempts = item.get("attempts", 0)

    if status == "failed" and attempts < max_attempts:
        return "retry"

    if idx + 1 < len(items):
        return "next"

    return "done"


def advance_next(state: AgentState) -> dict:
    return {"current_index": state["current_index"] + 1}


def deliver(state: AgentState) -> dict:
    """CLI-mode deliver: write test files to <repo>/generated_tests/ mirroring paths.

    M2 swaps this for SCM branch+PR creation.
    """
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
