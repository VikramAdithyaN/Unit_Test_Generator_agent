from __future__ import annotations

import hmac
import logging

import httpx

from app.config import settings
from app.models import PRPayload
from app.scm.base import SCMClient

BB_API = "https://api.bitbucket.org/2.0"

log = logging.getLogger(__name__)


class BitbucketClient(SCMClient):
    def __init__(self) -> None:
        self._auth = None
        if settings.bitbucket_username and settings.bitbucket_app_password:
            self._auth = (settings.bitbucket_username, settings.bitbucket_app_password)

    def _client(self) -> httpx.Client:
        return httpx.Client(auth=self._auth, timeout=30.0)

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        """Bitbucket Cloud webhooks don't sign by default. If a shared-secret
        header is configured, we require it. Otherwise best-effort."""
        if not settings.bitbucket_webhook_secret:
            log.warning("BITBUCKET_WEBHOOK_SECRET not set; skipping check")
            return True
        got = headers.get("x-hook-secret", "")
        return hmac.compare_digest(got, settings.bitbucket_webhook_secret)

    def parse_webhook(self, headers: dict, body_json: dict) -> PRPayload | None:
        event = headers.get("x-event-key", "")
        if event not in ("pullrequest:created", "pullrequest:updated"):
            return None

        pr = body_json.get("pullrequest") or {}
        repo = body_json.get("repository") or {}
        src = pr.get("source", {})
        dest = pr.get("destination", {})
        return PRPayload(
            scm="bitbucket",
            repo_full_name=repo["full_name"],
            pr_number=pr["id"],
            pr_title=pr.get("title") or "",
            pr_body=pr.get("description") or "",
            base_ref=dest.get("branch", {}).get("name", ""),
            base_sha=dest.get("commit", {}).get("hash", ""),
            head_ref=src.get("branch", {}).get("name", ""),
            head_sha=src.get("commit", {}).get("hash", ""),
            clone_url=self.clone_url_with_auth(repo["full_name"]),
            author=pr.get("author", {}).get("nickname", ""),
        )

    def clone_url_with_auth(self, repo_full_name: str) -> str:
        if not self._auth:
            return f"https://bitbucket.org/{repo_full_name}.git"
        user, pw = self._auth
        return f"https://{user}:{pw}@bitbucket.org/{repo_full_name}.git"

    def open_follow_up_pr(
        self, payload: PRPayload, source_branch: str, title: str, body: str
    ) -> str:
        url = f"{BB_API}/repositories/{payload.repo_full_name}/pullrequests"
        data = {
            "title": title,
            "description": body,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": payload.head_ref}},
        }
        with self._client() as c:
            r = c.post(url, json=data)
            r.raise_for_status()
            return r.json().get("links", {}).get("html", {}).get("href", "")

    def post_pr_comment(self, payload: PRPayload, body: str) -> None:
        url = (
            f"{BB_API}/repositories/{payload.repo_full_name}"
            f"/pullrequests/{payload.pr_number}/comments"
        )
        with self._client() as c:
            r = c.post(url, json={"content": {"raw": body}})
            r.raise_for_status()

    def post_pr_line_comment(
        self, payload: PRPayload, file_path: str, line: int, body: str
    ) -> None:
        url = (
            f"{BB_API}/repositories/{payload.repo_full_name}"
            f"/pullrequests/{payload.pr_number}/comments"
        )
        payload_data = {
            "content": {"raw": body},
            "inline": {"path": file_path, "to": line},
        }
        try:
            with self._client() as c:
                r = c.post(url, json=payload_data)
                r.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("bitbucket inline comment failed (%s); falling back", exc)
            self.post_pr_comment(payload, f"**{file_path}:{line}** — {body}")
