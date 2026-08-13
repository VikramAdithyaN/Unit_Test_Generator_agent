from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent.state import ExecutionResult, ExistingSuiteResult

PYTEST_TIMEOUT_SEC = 120
SUITE_TIMEOUT_SEC = 300


def _copy_repo(src: Path, dst: Path) -> None:
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"
        ),
    )


def _detect_runner(repo: Path) -> str | None:
    """Best-effort: figure out the target repo's test command."""
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or list(repo.rglob("test_*.py"))[:1]:
        return "pytest"
    if (repo / "package.json").exists():
        return "npm-test"
    if (repo / "pom.xml").exists():
        return "mvn-test"
    return None


_PYTEST_SUMMARY_RE = re.compile(
    r"(?:(\d+) passed)?(?:,? *(\d+) failed)?(?:,? *(\d+) errors?)?"
)


def _parse_pytest_summary(stdout: str) -> tuple[int, int]:
    """Return (passed, failed) counts by parsing the last summary line."""
    passed = failed = 0
    for line in reversed(stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            m = _PYTEST_SUMMARY_RE.search(line)
            if m:
                if m.group(1):
                    passed = int(m.group(1))
                if m.group(2):
                    failed = int(m.group(2))
                if m.group(3):
                    failed += int(m.group(3))
                break
    return passed, failed


def run_repo_test_suite(repo_path: str) -> ExistingSuiteResult:
    """Run the target repo's own existing test suite. Returns a summary."""
    repo = Path(repo_path).resolve()
    runner = _detect_runner(repo)

    if runner is None:
        return ExistingSuiteResult(
            ran=False, detected_runner="none",
            total=0, passed=0, failed=0,
            failed_tests=[], stdout="", stderr="no test runner detected",
        )

    if runner != "pytest":
        return ExistingSuiteResult(
            ran=False, detected_runner=runner,
            total=0, passed=0, failed=0,
            failed_tests=[],
            stdout="", stderr=f"runner {runner} not yet supported in M1b",
        )

    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            cwd=repo, capture_output=True, text=True,
            timeout=SUITE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        return ExistingSuiteResult(
            ran=True, detected_runner="pytest",
            total=0, passed=0, failed=0,
            failed_tests=[],
            stdout=exc.stdout or "",
            stderr=f"TIMEOUT after {SUITE_TIMEOUT_SEC}s",
        )
    except FileNotFoundError as exc:
        return ExistingSuiteResult(
            ran=False, detected_runner="pytest",
            total=0, passed=0, failed=0,
            failed_tests=[], stdout="", stderr=str(exc),
        )

    passed, failed = _parse_pytest_summary(proc.stdout)
    failed_tests = [
        line.split(" ")[1] for line in proc.stdout.splitlines()
        if line.startswith("FAILED ")
    ]

    return ExistingSuiteResult(
        ran=True, detected_runner="pytest",
        total=passed + failed, passed=passed, failed=failed,
        failed_tests=failed_tests,
        stdout=proc.stdout, stderr=proc.stderr,
    )


def run_pytest(repo_path: str, test_relpath: str, test_code: str) -> ExecutionResult:
    """Copy repo to a tempdir, drop the generated test in, run pytest on that file only."""
    src = Path(repo_path).resolve()
    if not src.exists():
        return ExecutionResult(passed=False, stdout="", stderr=f"repo not found: {src}")

    with tempfile.TemporaryDirectory(prefix="testgen-") as tmp:
        dst = Path(tmp) / "repo"
        _copy_repo(src, dst)

        test_file = dst / test_relpath
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(test_code)

        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", str(test_file), "-x", "--tb=short", "-q"],
                cwd=dst, capture_output=True, text=True,
                timeout=PYTEST_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                passed=False,
                stdout=exc.stdout or "",
                stderr=f"TIMEOUT after {PYTEST_TIMEOUT_SEC}s",
            )
        except FileNotFoundError as exc:
            return ExecutionResult(passed=False, stdout="", stderr=str(exc))

        return ExecutionResult(
            passed=proc.returncode == 0,
            stdout=proc.stdout, stderr=proc.stderr,
        )
