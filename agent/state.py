from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages

Tier = Literal["tier1", "tier2"]
Language = Literal["python", "javascript", "typescript", "java", "go", "ruby", "unknown"]


class ChangedFile(TypedDict):
    path: str
    language: Language
    tier: Tier
    content: str
    existing_tests: str | None


class ExecutionResult(TypedDict):
    passed: bool
    stdout: str
    stderr: str


class FileWorkItem(TypedDict, total=False):
    file: ChangedFile
    plan: str
    test_path: str
    test_code: str
    execution: ExecutionResult
    attempts: int
    status: Literal["pending", "generated", "validated", "failed", "unverified"]


class AgentState(TypedDict, total=False):
    repo_path: str
    validate: bool
    max_attempts: int

    work_items: list[FileWorkItem]
    current_index: int

    messages: Annotated[list, add_messages]
    errors: list[str]
