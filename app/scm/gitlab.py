from __future__ import annotations

import hmac
import logging

import gitlab

from app.config import settings
from app.models import PRPayload
from app.scm.base import SCMClient

log = logging.getLogger(__name__)


class GitLabClient(SCMClient):
    def __init__(self) -> None:
        self._gl = (
            gitlab.Gitlab(settings.gitlab_url, private_token=settings.gitlab_token)
            if settings.gitlab_token
            else None
        )

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        if not settings.gitlab_webhook_secret:
            log.warning("GITLAB_WEBHOOK_SECRET not set; skipping token check")
            return True
        token = headers.get("x-gitlab-token", "")
        return hmac.compare_digest(token, settings.gitlab_webhook_secret)

    def parse_webhook(self, headers: dict, body_json: dict) -> PRPayload | None:
        event = headers.get("x-gitlab-event", "")
        if event != "Merge Request Hook":
            return None
        attrs = body_json.get("object_attributes", {})
        action = attrs.get("action")
        if action not in ("open", "update", "reopen"):
            return None

        project = body_json["project"]
        user = body_json.get("user", {})
        return PRPayload(
            scm="gitlab",
            repo_full_name=project["path_with_namespace"],
            pr_number=attrs["iid"],
            pr_title=attrs.get("title") or "",
            pr_body=attrs.get("description") or "",
            base_ref=attrs["target_branch"],
            base_sha=attrs.get("last_commit", {}).get("id", "")
            or attrs.get("target", {}).get("sha", ""),
            head_ref=attrs["source_branch"],
            head_sha=attrs.get("last_commit", {}).get("id", ""),
            clone_url=self.clone_url_with_auth(project["path_with_namespace"]),
            author=user.get("username", ""),
            extra={"project_id": project["id"]},
        )

    def clone_url_with_auth(self, repo_full_name: str) -> str:
        token = settings.gitlab_token
        host = settings.gitlab_url.rstrip("/").replace("https://", "").replace("http://", "")
        if not token:
            return f"https://{host}/{repo_full_name}.git"
        return f"https://oauth2:{token}@{host}/{repo_full_name}.git"

    def _project(self, payload: PRPayload):
        assert self._gl, "GITLAB_TOKEN not configured"
        pid = payload.extra.get("project_id") or payload.repo_full_name
        return self._gl.projects.get(pid)

    def open_follow_up_pr(
        self, payload: PRPayload, source_branch: str, title: str, body: str
    ) -> str:
        project = self._project(payload)
        mr = project.mergerequests.create({
            "source_branch": source_branch,
            "target_branch": payload.head_ref,
            "title": title,
            "description": body,
        })
        return mr.web_url

    def post_pr_comment(self, payload: PRPayload, body: str) -> None:
        project = self._project(payload)
        mr = project.mergerequests.get(payload.pr_number)
        mr.notes.create({"body": body})

    def post_pr_line_comment(
        self, payload: PRPayload, file_path: str, line: int, body: str
    ) -> None:
        # GitLab position API is finicky; fall back to plain MR note tagged with location.
        self.post_pr_comment(payload, f"**{file_path}:{line}** — {body}")
