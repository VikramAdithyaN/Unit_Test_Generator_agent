from __future__ import annotations

from pathlib import Path

from agent.state import Language, Tier

EXTENSION_MAP: dict[str, Language] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
}

TIER1_LANGUAGES: set[Language] = {"python", "javascript", "typescript", "java"}


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
    return str(parent / f"{stem}.test{p.suffix}")


def read_source(repo_path: str, rel_path: str) -> str:
    return (Path(repo_path) / rel_path).read_text()


def find_existing_test(repo_path: str, source_rel: str, language: Language) -> str | None:
    candidate = Path(repo_path) / test_path_for(source_rel, language)
    if candidate.exists():
        return candidate.read_text()

    tests_dir = Path(repo_path) / "tests"
    if tests_dir.exists():
        stem = Path(source_rel).stem
        for match in tests_dir.rglob(f"*{stem}*"):
            if match.is_file():
                return match.read_text()
    return None
