from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from agent.state import ExecutionResult

PYTEST_TIMEOUT_SEC = 120


def run_pytest(repo_path: str, test_relpath: str, test_code: str) -> ExecutionResult:
    """Copy the repo to a tempdir, write the test file at test_relpath, run pytest.

    This is the M1 local executor. In M3 we swap this for a Docker-based sandbox.
    """
    src = Path(repo_path).resolve()
    if not src.exists():
        return ExecutionResult(passed=False, stdout="", stderr=f"repo not found: {src}")

    with tempfile.TemporaryDirectory(prefix="testgen-") as tmp:
        dst = Path(tmp) / "repo"
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "node_modules"),
        )

        test_file = dst / test_relpath
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(test_code)

        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", str(test_file), "-x", "--tb=short", "-q"],
                cwd=dst,
                capture_output=True,
                text=True,
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
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
