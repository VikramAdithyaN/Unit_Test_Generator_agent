from __future__ import annotations

import logging
from pathlib import Path

from agent.state import Language, Tier

log = logging.getLogger(__name__)

EXTENSION_MAP: dict[str, Language] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".cs": "csharp",
    ".go": "go",
    ".rb": "ruby",
}

# First-class: tests get generated with idiomatic framework conventions.
# Sandbox execution (validation loop) is currently Python-only; the rest are
# generated-only until the Docker sandbox lands (M4).
TIER1_LANGUAGES: set[Language] = {
    "python", "javascript", "typescript", "java", "csharp",
}


def detect_language(path: str) -> Language:
    ext = Path(path).suffix.lower()
    return EXTENSION_MAP.get(ext, "unknown")


def tier_for(language: Language) -> Tier:
    return "tier1" if language in TIER1_LANGUAGES else "tier2"


def test_path_for(source_path: str, language: Language) -> str:
    p = Path(source_path)
    stem = p.stem
    parent = p.parent

    if language == "python":
        return str(parent / f"test_{stem}.py")
    if language in ("javascript", "typescript"):
        suffix = ".ts" if language == "typescript" else ".js"
        return str(parent / f"{stem}.test{suffix}")
    if language == "java":
        return str(parent / f"{stem}Test.java")
    if language == "csharp":
        return str(parent / f"{stem}Tests.cs")
    return str(parent / f"{stem}.test{p.suffix}")


def read_source(repo_path: str, rel_path: str) -> str:
    return (Path(repo_path) / rel_path).read_text()


TEST_SUFFIXES_BY_LANG: dict[Language, tuple[str, ...]] = {
    "python":     ("_test.py",),
    "javascript": (".test.js", ".test.jsx", ".test.mjs", ".test.cjs",
                   ".spec.js", ".spec.jsx", ".spec.mjs", ".spec.cjs"),
    "typescript": (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"),
    "java":       ("Test.java", "Tests.java"),
    "csharp":     ("Tests.cs", "Test.cs"),
    "go":         ("_test.go",),
    "ruby":       ("_spec.rb", "_test.rb"),
    "unknown":    (),
}

TEST_DIR_NAMES = ("tests", "test", "spec", "__tests__", "Tests", "Test")

MAX_EXISTING_TEST_CHARS = 20_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             "dist", "build", "target", "bin", "obj", ".pytest_cache"}


def _test_filename_candidates(stem: str, language: Language) -> set[str]:
    """All idiomatic test filenames for a source stem in this language."""
    names: set[str] = set()
    if language == "python":
        names.add(f"test_{stem}.py")
        names.add(f"{stem}_test.py")
    if language in ("javascript", "typescript"):
        for s in TEST_SUFFIXES_BY_LANG[language]:
            names.add(f"{stem}{s}")
    if language == "java":
        names.add(f"{stem}Test.java")
        names.add(f"{stem}Tests.java")
    if language == "csharp":
        names.add(f"{stem}Tests.cs")
        names.add(f"{stem}Test.cs")
    if language == "go":
        names.add(f"{stem}_test.go")
    if language == "ruby":
        names.add(f"{stem}_spec.rb")
        names.add(f"{stem}_test.rb")
    return names


def _iter_search_roots(repo: Path, source_parent: Path) -> list[Path]:
    """Places to look for test files, ordered from most-specific to least."""
    roots: list[Path] = []
    if source_parent.exists():
        roots.append(source_parent)                          # colocated
        roots.append(source_parent / "__tests__")            # jest colocated
    for name in TEST_DIR_NAMES:
        p = repo / name
        if p.exists():
            roots.append(p)
    return [r for r in roots if r.exists()]


def find_existing_test(
    repo_path: str, source_rel: str, language: Language
) -> tuple[str | None, str | None]:
    """Locate existing test files for `source_rel`.

    Returns a tuple `(target_rel_path, all_content)`:
      * `target_rel_path` is the single test file we will MODIFY IN PLACE
        (append new cases to) — the primary/most colocated match.
      * `all_content` concatenates the content of every discovered test file
        (each prefixed with `// ===== path =====`) so the LLM sees the full
        surface when deciding whether a gap is genuine.

    Returns `(None, None)` if nothing is found — the caller then falls back to
    generating a brand-new test file at the language's default path.
    """
    repo = Path(repo_path)
    src = Path(source_rel)
    stem = src.stem
    source_parent = repo / src.parent
    wanted_names = _test_filename_candidates(stem, language)

    matches: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        matches.append(p)

    for root in _iter_search_roots(repo, source_parent):
        for name in wanted_names:
            hit = root / name
            if hit.exists() and hit.is_file():
                _add(hit)

    suffixes = TEST_SUFFIXES_BY_LANG.get(language, ())
    if suffixes:
        for root in _iter_search_roots(repo, source_parent):
            for p in root.rglob(f"*{stem}*"):
                if not p.is_file():
                    continue
                if any(part in SKIP_DIRS for part in p.parts):
                    continue
                if any(p.name.endswith(s) for s in suffixes):
                    _add(p)

    if not matches:
        log.info("no existing tests found for %s", source_rel)
        return None, None

    log.info(
        "existing tests for %s: %s (target=%s)",
        source_rel,
        [str(m.relative_to(repo)) for m in matches],
        str(matches[0].relative_to(repo)),
    )
    target_rel = str(matches[0].relative_to(repo))

    chunks: list[str] = []
    total = 0
    for m in matches:
        try:
            content = m.read_text()
        except (UnicodeDecodeError, OSError) as exc:
            log.debug("skipping unreadable %s: %s", m, exc)
            continue
        header = f"\n\n// ===== {m.relative_to(repo)} =====\n"
        remaining = MAX_EXISTING_TEST_CHARS - total
        if remaining <= 0:
            break
        chunk = header + content
        if len(chunk) > remaining:
            chunk = chunk[:remaining] + "\n// ... (truncated)"
        chunks.append(chunk)
        total += len(chunk)

    return target_rel, ("".join(chunks) if chunks else None)


def read_test_file(repo_path: str, target_rel: str) -> str:
    """Read the raw content of a single test file, preserving line endings.

    IMPORTANT: `Path.read_text()` translates CRLF → LF via universal newlines.
    For test files created on Windows/Node projects that's a silent bug — we
    write back LF, git sees every line as changed, and the follow-up MR shows
    a full-file replacement. Reading with `newline=''` disables translation
    and preserves the original CRLF/LF exactly.
    """
    p = Path(repo_path) / target_rel
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()
