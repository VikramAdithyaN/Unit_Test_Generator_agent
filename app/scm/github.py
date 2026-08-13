from __future__ import annotations

import hashlib
import hmac
import logging

from github import Github

from app.config import settings
from app.models import PRPayload
from app.scm.base import SCMClient

log = logging.getLogger(__name__)


class GitHubClient(SCMClient):
    def __init__(self) -> None:
        self._gh = Github(settings.github_token) if settings.github_token else None

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        if not settings.github_webhook_secret:
            log.warning("GITHUB_WEBHOOK_SECRET not set; skipping signature check")
            return True
        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            return False
        digest = hmac.new(
            settings.github_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature.split("=", 1)[1], digest)

    def parse_webhook(self, headers: dict, body_json: dict) -> PRPayload | None:
        event = headers.get("x-github-event", "")
        if event != "pull_request":
            return None
        action = body_json.get("action")
        if action not in ("opened", "synchronize", "reopened"):
            return None

        pr = body_json["pull_request"]
        repo = body_json["repository"]
        return PRPayload(
            scm="github",
            repo_full_name=repo["full_name"],
            pr_number=pr["number"],
            pr_title=pr.get("title") or "",
            pr_body=pr.get("body") or "",
            base_ref=pr["base"]["ref"],
            base_sha=pr["base"]["sha"],
            head_ref=pr["head"]["ref"],
            head_sha=pr["head"]["sha"],
            clone_url=self.clone_url_with_auth(repo["full_name"]),
            author=pr["user"]["login"],
        )

    def clone_url_with_auth(self, repo_full_name: str) -> str:
        token = settings.github_token
        if not token:
            return f"https://github.com/{repo_full_name}.git"
        return f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

    def _repo(self, payload: PRPayload):
        assert self._gh, "GITHUB_TOKEN not configured"
        return self._gh.get_repo(payload.repo_full_name)

    def open_follow_up_pr(
        self, payload: PRPayload, source_branch: str, title: str, body: str
    ) -> str:
        repo = self._repo(payload)
        pr = repo.create_pull(
            title=title, body=body, base=payload.head_ref, head=source_branch
        )
        return pr.html_url

    def post_pr_comment(self, payload: PRPayload, body: str) -> None:
        repo = self._repo(payload)
        issue = repo.get_issue(payload.pr_number)
        issue.create_comment(body)

    def post_pr_line_comment(
        self, payload: PRPayload, file_path: str, line: int, body: str
    ) -> None:
        """Best-effort inline comment. Falls back to plain PR comment if the line
        isn't part of the diff (GitHub requires the line to be in the diff)."""
        repo = self._repo(payload)
        try:
            pr = repo.get_pull(payload.pr_number)
            commit = repo.get_commit(payload.head_sha)
            pr.create_review_comment(
                body=body, commit=commit, path=file_path, line=line, side="RIGHT"
            )
        except Exception as exc:  # noqa: BLE001
            log.info("inline comment failed (%s); falling back to PR comment", exc)
            self.post_pr_comment(payload, f"**{file_path}:{line}** — {body}")
