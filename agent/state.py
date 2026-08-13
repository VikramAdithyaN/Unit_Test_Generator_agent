from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages

Tier = Literal["tier1", "tier2"]
Language = Literal["python", "javascript", "typescript", "java", "go", "ruby", "unknown"]

FailureVerdict = Literal["intentional", "suspicious", "unknown"]
WorkStatus = Literal[
    "pending",
    "no_gaps",
    "generated",
    "validated",
    "failed",
    "unverified",
]


class PRContext(TypedDict, total=False):
    title: str
    body: str
    base_ref: str
    head_ref: str


class ExistingSuiteResult(TypedDict, total=False):
    ran: bool
    detected_runner: str
    total: int
    passed: int
    failed: int
    failed_tests: list[str]
    stdout: str
    stderr: str


class ChangedFile(TypedDict):
    path: str
    language: Language
    tier: Tier
    base_content: str | None
    head_content: str | None
    existing_tests: str | None


class CoverageGap(TypedDict):
    what: str
    why: str


class ExecutionResult(TypedDict):
    passed: bool
    stdout: str
    stderr: str


class FailureClassification(TypedDict):
    verdict: FailureVerdict
    reasoning: str


class FileWorkItem(TypedDict, total=False):
    file: ChangedFile
    coverage_gaps: list[CoverageGap]
    plan: str
    test_path: str
    test_code: str
    execution: ExecutionResult
    attempts: int
    status: WorkStatus
    failure_classification: FailureClassification


class AgentState(TypedDict, total=False):
    repo_path: str
    pr_context: PRContext
    validate: bool
    max_attempts: int
    deliver_mode: Literal["cli", "scm"]

    existing_suite: ExistingSuiteResult
    work_items: list[FileWorkItem]
    current_index: int

    messages: Annotated[list, add_messages]
    errors: list[str]
