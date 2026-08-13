from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def resolve_ref(repo: str | Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).strip()


def changed_files_between(
    repo: str | Path, base: str, head: str
) -> list[str]:
    """Return repo-relative paths of files changed between two refs (added/modified/renamed).

    Deletions are excluded because there's nothing to test.
    """
    out = _git(repo, "diff", "--name-status", f"{base}..{head}")
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("D"):
            continue
        files.append(parts[-1])
    return files


def read_file_at_ref(repo: str | Path, ref: str, rel_path: str) -> str | None:
    """Return file content at a given ref. None if the file didn't exist there."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{rel_path}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def checkout_worktree(repo: str | Path, ref: str, dest: str | Path) -> None:
    """Materialize the given ref as a working tree at `dest` (git worktree add)."""
    _git(repo, "worktree", "add", "--detach", str(dest), ref)


def remove_worktree(repo: str | Path, dest: str | Path) -> None:
    try:
        _git(repo, "worktree", "remove", "--force", str(dest))
    except GitError:
        pass
