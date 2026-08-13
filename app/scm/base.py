from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import PRPayload


class SCMClient(ABC):
    """Abstract adapter each SCM provider implements."""

    @abstractmethod
    def verify_signature(self, headers: dict, body: bytes) -> bool:
        """Return True iff the webhook signature is valid."""

    @abstractmethod
    def parse_webhook(self, headers: dict, body_json: dict) -> PRPayload | None:
        """Return a PRPayload for actionable PR events; None to ignore."""

    @abstractmethod
    def clone_url_with_auth(self, repo_full_name: str) -> str:
        """A https clone URL with token embedded, safe to pass to git clone."""

    @abstractmethod
    def open_follow_up_pr(
        self,
        payload: PRPayload,
        source_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR from source_branch targeting the original PR's head branch. Returns URL."""

    @abstractmethod
    def post_pr_comment(self, payload: PRPayload, body: str) -> None:
        """Post a general comment on the original PR."""

    @abstractmethod
    def post_pr_line_comment(
        self,
        payload: PRPayload,
        file_path: str,
        line: int,
        body: str,
    ) -> None:
        """Post a review comment tied to a specific line in the diff."""
