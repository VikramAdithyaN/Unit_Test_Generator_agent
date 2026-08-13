from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from git import Repo

log = logging.getLogger(__name__)


def clone_pr(clone_url: str, base_ref: str, head_ref: str, head_sha: str) -> Path:
    """Clone the repo into a fresh tempdir with both base and head refs available.
    Checks out the head ref. Returns the local repo path."""
    tmp = Path(tempfile.mkdtemp(prefix="testgen-clone-"))
    log.info("cloning to %s", tmp)
    repo = Repo.clone_from(clone_url, tmp, no_single_branch=True)

    # Ensure both refs are fetched
    origin = repo.remotes.origin
    origin.fetch()

    try:
        repo.git.checkout(head_sha)
    except Exception:
        repo.git.checkout(head_ref)
    return tmp


def push_tests_branch(
    repo_path: Path,
    new_branch: str,
    from_ref: str,
    files: dict[str, str],
    commit_message: str,
    author_name: str = "testgen-bot",
    author_email: str = "testgen-bot@example.com",
) -> None:
    """Create a new branch from `from_ref`, write files, commit, push."""
    repo = Repo(repo_path)
    repo.git.checkout(from_ref)
    repo.git.checkout("-b", new_branch)

    written: list[str] = []
    for rel_path, content in files.items():
        dest = repo_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        written.append(rel_path)

    if not written:
        return

    repo.index.add(written)
    with repo.config_writer() as w:
        w.set_value("user", "name", author_name)
        w.set_value("user", "email", author_email)
    repo.index.commit(commit_message)
    origin = repo.remotes.origin
    origin.push(refspec=f"HEAD:{new_branch}")


def cleanup(repo_path: Path) -> None:
    shutil.rmtree(repo_path, ignore_errors=True)
