from __future__ import annotations

from app.models import SCM
from app.scm.base import SCMClient
from app.scm.bitbucket import BitbucketClient
from app.scm.github import GitHubClient
from app.scm.gitlab import GitLabClient


def get_client(scm: SCM) -> SCMClient:
    if scm == "github":
        return GitHubClient()
    if scm == "gitlab":
        return GitLabClient()
    if scm == "bitbucket":
        return BitbucketClient()
    raise ValueError(f"unsupported SCM: {scm}")
