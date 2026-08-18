from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SCM = Literal["github", "gitlab", "bitbucket"]


@dataclass
class PRPayload:
    scm: SCM
    repo_full_name: str
    pr_number: int
    pr_title: str
    pr_body: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    clone_url: str
    author: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class DeliveryReport:
    pr_number: int
    scm: SCM
    tests_pr_url: str = ""
    tests_branch: str = ""
    files_generated: list[str] = field(default_factory=list)
    files_skipped_covered: list[str] = field(default_factory=list)
    suspicious_flags: list[dict] = field(default_factory=list)
    # {source_file, test_file, identifiers[]} — reviewer should update or delete.
    stale_test_refs: list[dict] = field(default_factory=list)
    existing_suite_summary: str = ""
