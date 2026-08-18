from __future__ import annotations

import difflib
import json
import logging
import os
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

log = logging.getLogger(__name__)

from agent import git_utils
from agent.prompts import (
    CLASSIFIER_SYSTEM_V1,
    GAP_ANALYZER_SYSTEM_V1,
    GENERATOR_APPEND_SYSTEM_V1,
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
    read_test_file,
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

        target_test_rel, existing = find_existing_test(repo, rel, language)

        cf: ChangedFile = {
            "path": rel,
            "language": language,
            "tier": tier,
            "base_content": base_content,
            "head_content": head_content,
            "existing_tests": existing,
        }
        if target_test_rel:
            mode = "append"
            test_path = target_test_rel
            existing_raw = read_test_file(repo, target_test_rel)
            log.info(
                "ingest %s: mode=append target=%s existing=%d bytes",
                rel, test_path, len(existing_raw),
            )
        else:
            mode = "new"
            test_path = test_path_for(rel, language)
            existing_raw = None
            log.info("ingest %s: mode=new fresh_path=%s", rel, test_path)

        stale = _find_stale_test_refs(
            base_content or "", head_content or "", existing_raw or "",
        )
        if stale:
            log.warning(
                "stale test refs in %s: %s (identifiers removed from source but "
                "still referenced by %s)",
                rel, stale, target_test_rel,
            )

        work_items.append(
            FileWorkItem(
                file=cf,
                attempts=0,
                status="pending",
                test_path=test_path,
                mode=mode,
                existing_test_content=existing_raw,
                stale_refs=stale,
                coverage_gaps=[],
            )
        )

    return {"work_items": work_items, "current_index": 0, "errors": errors}


def run_existing_suite(state: AgentState) -> dict:
    """Run the target repo's own test suite against HEAD."""
    result = run_repo_test_suite(state["repo_path"])
    return {"existing_suite": result}


_GAP_LINE_RE = re.compile(r"^-\s*(.+?):\s*(.+)$")


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _extract_identifier(gap_what: str) -> str | None:
    """Pull the leading identifier out of a gap label. E.g. 'calculateTax()' -> 'calculateTax'."""
    m = _IDENT_RE.search(gap_what)
    return m.group(0) if m else None


def _detect_eol(text: str) -> str:
    """Return the dominant line ending in `text` ('\\r\\n' or '\\n').
    Used so that append-mode additions round-trip in the same line-ending
    style as the existing file (otherwise git shows a full-file rewrite)."""
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _match_eol(text: str, target_eol: str) -> str:
    """Force `text` to use `target_eol` regardless of what it currently uses."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target_eol != "\n":
        normalized = normalized.replace("\n", target_eol)
    return normalized


def _extract_additions(existing: str, candidate: str) -> str:
    """Given the existing test file and an LLM output that MAY be either
    'just additions' or a full-file rewrite, return only the lines that are
    genuine additions (present in candidate, not in existing).

    Uses SequenceMatcher edit ops. Preserves the ordering the LLM chose.
    Returns "" if the candidate is a strict subset of existing (nothing new).
    """
    existing_lines = existing.splitlines()
    candidate_lines = candidate.splitlines()
    matcher = difflib.SequenceMatcher(a=existing_lines, b=candidate_lines, autojunk=False)
    added: list[str] = []
    for op, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if op in ("insert", "replace"):
            added.extend(candidate_lines[j1:j2])
    while added and not added[0].strip():
        added.pop(0)
    while added and not added[-1].strip():
        added.pop()
    return "\n".join(added)


_FULL_FILE_HINTS = ("import ", "require(", "from ", "using ", "package ")

# Reserved words we never treat as "identifiers the diff removed" — otherwise
# any refactor that drops `if` / `return` would generate false alarms.
_LANG_KEYWORDS = {
    "if", "else", "elif", "return", "function", "const", "let", "var", "def",
    "class", "import", "from", "for", "while", "true", "false", "null", "None",
    "True", "False", "undefined", "public", "private", "protected", "static",
    "void", "int", "string", "bool", "new", "this", "self", "super", "async",
    "await", "yield", "throw", "try", "catch", "finally", "except", "raise",
    "with", "in", "is", "and", "or", "not", "typeof", "instanceof", "as",
    "namespace", "using", "package", "extends", "implements", "interface",
    "abstract", "final", "override", "readonly", "get", "set", "of", "do",
    "switch", "case", "break", "continue", "default", "export", "type",
}


def _find_stale_test_refs(base: str, head: str, existing_tests: str) -> list[str]:
    """Identifiers that BASE source declared but HEAD source no longer contains,
    and that still appear in the existing test file. Likely stale references
    (removed/renamed function) — surfaced to the reviewer, never edited by us."""
    if not base or not existing_tests:
        return []
    base_idents = set(_IDENT_RE.findall(base))
    head_idents = set(_IDENT_RE.findall(head))
    removed = {
        r for r in (base_idents - head_idents)
        if len(r) > 2 and r not in _LANG_KEYWORDS and not r.startswith("_")
    }
    stale = [
        ident for ident in removed
        if re.search(rf"\b{re.escape(ident)}\b", existing_tests)
    ]
    return sorted(stale)


def _looks_like_full_rewrite(existing: str, candidate: str) -> bool:
    """Heuristic: did the LLM ignore 'output only additions' and emit a full
    test file? Signs: candidate starts with an import/require/using, OR its
    first ~40 lines share > 70% content with the existing file's first ~40."""
    stripped = candidate.lstrip()
    for hint in _FULL_FILE_HINTS:
        if stripped.startswith(hint):
            return True
    existing_head = "\n".join(existing.splitlines()[:40])
    candidate_head = "\n".join(candidate.splitlines()[:40])
    if not existing_head or not candidate_head:
        return False
    ratio = difflib.SequenceMatcher(a=existing_head, b=candidate_head, autojunk=False).ratio()
    return ratio > 0.7


def compute_coverage_gaps(state: AgentState) -> dict:
    """LLM: for each file, list coverage gaps the diff touches.
    After the LLM answers, we drop any gap whose identifier literally appears
    in the existing test file content — a hard guard against hallucinated gaps."""
    items = state["work_items"]
    pr = state.get("pr_context", {})
    llm = _llm()

    for i, item in enumerate(items):
        file = item["file"]
        if not file["base_content"] and not file["head_content"]:
            continue

        existing = file["existing_tests"] or ""
        existing_block = (
            f"Existing tests already in the repo (multiple files may be shown, "
            f"each with its own '// ===== path =====' header). If any function/"
            f"method name below appears in these tests, treat it as covered.\n"
            f"```\n{existing[:16000]}\n```"
            if existing
            else "No existing tests were found for this file."
        )
        user = (
            f"Language: {file['language']}\n"
            f"Path: {file['path']}\n\n"
            f"PR title: {pr.get('title', '(none)')}\n"
            f"PR body: {pr.get('body', '(none)')[:2000]}\n\n"
            f"BASE version:\n```\n{(file['base_content'] or '(file did not exist)')[:6000]}\n```\n\n"
            f"HEAD version:\n```\n{(file['head_content'] or '(file deleted)')[:6000]}\n```\n\n"
            f"{existing_block}\n"
        )
        resp = llm.invoke([
            SystemMessage(content=GAP_ANALYZER_SYSTEM_V1),
            HumanMessage(content=user),
        ])
        text = resp.content.strip()

        raw_gaps: list[CoverageGap] = []
        if text.upper() != "NONE":
            for line in text.splitlines():
                m = _GAP_LINE_RE.match(line.strip())
                if m:
                    raw_gaps.append(
                        CoverageGap(what=m.group(1).strip(), why=m.group(2).strip())
                    )

        # Hard post-check: if the identifier the LLM named is present verbatim
        # in the existing test content, drop that gap. This kills hallucinated
        # "add more edge cases" style gaps.
        gaps: list[CoverageGap] = []
        for g in raw_gaps:
            ident = _extract_identifier(g["what"])
            if ident and existing and re.search(rf"\b{re.escape(ident)}\b", existing):
                log.info(
                    "dropping hallucinated gap for %s: identifier %r found in existing tests",
                    file["path"], ident,
                )
                continue
            gaps.append(g)

        log.info(
            "gaps for %s: llm_raw=%d kept=%d %s",
            file["path"], len(raw_gaps), len(gaps),
            [g["what"] for g in gaps],
        )

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
    """LLM: write regression tests from BASE code.
    Append-mode: LLM emits only new cases; we splice them into the existing
    file so git records a real diff (additions, not a rewrite).
    New-mode: LLM emits a whole file (current behavior).
    Retry path (attempt > 0): full-file revision regardless of mode."""
    items = state["work_items"]
    idx = state["current_index"]
    item = items[idx]
    file = item["file"]
    attempt = item.get("attempts", 0)
    mode = item.get("mode", "new")
    base = file["base_content"] or file["head_content"] or ""
    head = file["head_content"] or ""

    if attempt > 0:
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
    elif mode == "append":
        existing_raw = item.get("existing_test_content") or ""
        log.info(
            "append mode: %s target=%s existing=%d bytes",
            file["path"], item.get("test_path"), len(existing_raw),
        )
        if not existing_raw:
            # This should not happen — ingest_pr populated the field. Log
            # loudly and fall through to no-op rather than clobber the file.
            log.error(
                "append mode: %s has empty existing_test_content; refusing to write "
                "(would clobber the user's test file). Marking no_gaps.",
                file["path"],
            )
            items[idx] = {**item, "test_code": "", "attempts": attempt + 1, "status": "no_gaps"}
            return {"work_items": items}

        gaps_str = "\n".join(
            f"- {g['what']}: {g['why']}" for g in item.get("coverage_gaps", [])
        )
        system = GENERATOR_APPEND_SYSTEM_V1
        user = (
            f"Language: {file['language']}\n"
            f"Source path: {file['path']}\n"
            f"Existing test file: {item['test_path']}\n\n"
            f"Coverage gaps (only cover these):\n{gaps_str}\n\n"
            f"BASE source (before PR):\n```\n{base[:6000]}\n```\n\n"
            f"HEAD source (after PR):\n```\n{head[:6000]}\n```\n\n"
            f"EXISTING test file:\n```\n{existing_raw[:8000]}\n```\n"
        )
        resp = _llm().invoke([SystemMessage(content=system), HumanMessage(content=user)])
        raw_output = _strip_fences(resp.content).strip()
        log.info("append LLM output for %s: %d bytes", file["path"], len(raw_output))

        if not raw_output:
            log.info("append mode produced empty output for %s; skipping", file["path"])
            items[idx] = {**item, "test_code": "", "attempts": attempt + 1, "status": "no_gaps"}
            return {"work_items": items}

        # Safety: if the LLM ignored the prompt and emitted a full-file rewrite,
        # strip it back down to only the added lines so git records a clean
        # additions-only diff. Never overwrite the user's existing tests.
        if _looks_like_full_rewrite(existing_raw, raw_output):
            log.warning(
                "append mode: LLM emitted a full-file rewrite for %s (%d bytes); "
                "extracting additions only",
                file["path"], len(raw_output),
            )
            addition = _extract_additions(existing_raw, raw_output)
            log.info("extracted additions for %s: %d bytes", file["path"], len(addition))
        else:
            addition = raw_output

        if not addition.strip():
            log.info("no genuine additions for %s; skipping", file["path"])
            items[idx] = {**item, "test_code": "", "attempts": attempt + 1, "status": "no_gaps"}
            return {"work_items": items}

        eol = _detect_eol(existing_raw)
        addition = _match_eol(addition, eol)
        if existing_raw.endswith(eol * 2):
            sep = ""
        elif existing_raw.endswith(eol):
            sep = eol
        else:
            sep = eol * 2
        test_code = existing_raw + sep + addition + eol
        log.info(
            "append merged %s: existing=%d + additions=%d = %d bytes (eol=%r)",
            file["path"], len(existing_raw), len(addition), len(test_code),
            eol,
        )
    else:
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
