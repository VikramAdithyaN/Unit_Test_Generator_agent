from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from git import GitCommandError, Repo

log = logging.getLogger(__name__)


class PushFailed(RuntimeError):
    """Raised when the remote rejects the tests-branch push. Message is the
    exact git stderr so the reviewer knows why (protected branch, push rules,
    signed commits, etc.)."""


def clone_pr(clone_url: str, base_ref: str, head_ref: str, head_sha: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="testgen-clone-"))
    log.info("cloning to %s", tmp)
    repo = Repo.clone_from(clone_url, tmp, no_single_branch=True)
    repo.remotes.origin.fetch()

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
    author_email: str = "vikram.naralasetty@corpay.com",
) -> None:
    """Create a new branch from `from_ref`, write files, commit, push.
    Raises PushFailed with the git stderr if the remote rejects the push."""
    repo = Repo(repo_path)
    repo.git.checkout(from_ref)
    repo.git.checkout("-b", new_branch)

    written: list[str] = []
    for rel_path, content in files.items():
        dest = repo_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        # newline="" disables the LF → os.linesep translation that
        # write_text() does. Combined with read_test_file preserving the
        # original CRLF/LF, an append-mode merge round-trips byte-exact and
        # git records only the added lines instead of a full-file rewrite.
        with dest.open("w", encoding="utf-8", newline="") as f:
            f.write(content)
        written.append(rel_path)

    if not written:
        log.warning("no test files written; skipping commit + push")
        return

    repo.index.add(written)
    with repo.config_writer() as w:
        w.set_value("user", "name", author_name)
        w.set_value("user", "email", author_email)
    commit = repo.index.commit(commit_message)
    log.info("committed %d files as %s on branch %s", len(written), commit.hexsha[:7], new_branch)

    # Use the porcelain `git push` so we capture stderr and get a non-zero
    # exit code when the push is rejected (Repo.remotes.origin.push swallows
    # rejections into PushInfo flags that we then have to inspect).
    try:
        push_output = repo.git.push("--set-upstream", "origin", f"HEAD:{new_branch}")
        log.info("push ok: %s", push_output.strip() or "(silent)")
    except GitCommandError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        log.error("push rejected. stderr:\n%s", stderr)
        raise PushFailed(stderr or stdout or str(exc)) from exc


def cleanup(repo_path: Path) -> None:
    shutil.rmtree(repo_path, ignore_errors=True)
