from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from agent import git_utils
from agent.graph import build_graph
from agent.state import ChangedFile, FileWorkItem
from agent.tools import detect_language

app = typer.Typer(add_completion=False, help="LangGraph unit-test generator")
console = Console()

SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb"}


def _looks_like_test(path: str) -> bool:
    p = path.lower()
    return (
        "/test" in p
        or p.startswith("test")
        or p.endswith("_test.py")
        or p.endswith(".test.js")
        or p.endswith(".test.ts")
        or p.endswith(".spec.js")
        or p.endswith(".spec.ts")
    )


@app.command()
def generate(
    repo: Annotated[Path, typer.Option(help="Path to the target git repository")],
    base: Annotated[str, typer.Option(help="Base ref (e.g. main)")] = "main",
    head: Annotated[str, typer.Option(help="Head ref")] = "HEAD",
    files: Annotated[
        list[str] | None,
        typer.Option("--file", "-f", help="Explicit repo-relative source file (repeatable). If omitted, uses git diff."),
    ] = None,
    pr_title: Annotated[str, typer.Option(help="PR title (for intent classification)")] = "",
    pr_body: Annotated[str, typer.Option(help="PR body (for intent classification)")] = "",
    validate: Annotated[bool, typer.Option("--validate/--no-validate")] = True,
    max_attempts: Annotated[int, typer.Option(help="Retry cap per file")] = 3,
) -> None:
    """Generate regression tests for a PR (base..head diff)."""
    load_dotenv()

    if not repo.exists():
        console.print(f"[red]Repo not found:[/red] {repo}")
        raise typer.Exit(1)

    repo_abs = str(repo.resolve())

    if not files:
        try:
            all_changed = git_utils.changed_files_between(repo_abs, base, head)
        except git_utils.GitError as exc:
            console.print(f"[red]git diff failed:[/red] {exc}")
            raise typer.Exit(1)
        files = [
            f for f in all_changed
            if Path(f).suffix.lower() in SOURCE_EXTS and not _looks_like_test(f)
        ]
        if not files:
            console.print("[yellow]No source files changed between refs. Nothing to do.[/yellow]")
            raise typer.Exit(0)

    console.print(f"[cyan]Changed source files:[/cyan] {files}")

    seed_items: list[FileWorkItem] = [
        FileWorkItem(
            file=ChangedFile(
                path=f,
                language=detect_language(f),
                tier="tier2",
                base_content=None,
                head_content=None,
                existing_tests=None,
            )
        )
        for f in files
    ]

    graph = build_graph()
    final_state = graph.invoke(
        {
            "repo_path": repo_abs,
            "pr_context": {
                "title": pr_title,
                "body": pr_body,
                "base_ref": base,
                "head_ref": head,
            },
            "validate": validate,
            "max_attempts": max_attempts,
            "deliver_mode": "cli",
            "work_items": seed_items,
            "current_index": 0,
            "errors": [],
        },
        config={"recursion_limit": 200},
    )

    # Existing suite report
    suite = final_state.get("existing_suite", {})
    if suite.get("ran"):
        color = "green" if suite.get("failed", 0) == 0 else "red"
        console.print(
            f"[{color}]Existing suite ({suite.get('detected_runner')}):[/{color}] "
            f"{suite.get('passed', 0)} passed / {suite.get('failed', 0)} failed"
        )
        if suite.get("failed_tests"):
            console.print("[red]Existing tests that FAILED on head:[/red]")
            for t in suite["failed_tests"]:
                console.print(f"  - {t}")
    else:
        console.print(f"[yellow]Existing suite not run:[/yellow] {suite.get('stderr', 'unknown')}")

    # Per-file result
    table = Table(title="Test generation results")
    table.add_column("File")
    table.add_column("Lang")
    table.add_column("Gaps")
    table.add_column("Attempts")
    table.add_column("Status")
    table.add_column("Verdict")
    table.add_column("Test path")
    for item in final_state["work_items"]:
        cls = item.get("failure_classification") or {}
        table.add_row(
            item["file"]["path"],
            item["file"]["language"],
            str(len(item.get("coverage_gaps", []))),
            str(item.get("attempts", 0)),
            item.get("status", "?"),
            cls.get("verdict", "-"),
            item.get("test_path", ""),
        )
    console.print(table)

    # Suspicious flags
    for item in final_state["work_items"]:
        cls = item.get("failure_classification")
        if cls and cls.get("verdict") == "suspicious":
            console.print(
                f"[bold red]⚠ Suspicious change in {item['file']['path']}:[/bold red] "
                f"{cls.get('reasoning', '')}"
            )
        elif cls and cls.get("verdict") == "intentional":
            console.print(
                f"[green]Intentional change in {item['file']['path']}:[/green] "
                f"{cls.get('reasoning', '')}"
            )

    if final_state.get("errors"):
        console.print("[yellow]Warnings:[/yellow]")
        for err in final_state["errors"]:
            console.print(f"  - {err}")

    console.print(f"\n[green]Tests written to:[/green] {repo}/generated_tests/")


@app.command()
def simulate(
    scm: Annotated[str, typer.Option(help="github | gitlab | bitbucket")] = "github",
    repo_full_name: Annotated[str, typer.Option(help="org/repo")] = "",
    pr_number: Annotated[int, typer.Option(help="PR number")] = 1,
    clone_url: Annotated[str, typer.Option(help="git clone URL for the repo (must be reachable)")] = "",
    base_ref: Annotated[str, typer.Option(help="Base branch name")] = "main",
    head_ref: Annotated[str, typer.Option(help="Head branch name")] = "",
    head_sha: Annotated[str, typer.Option(help="Head SHA (or leave empty to resolve after clone)")] = "",
    pr_title: Annotated[str, typer.Option(help="PR title")] = "",
    pr_body: Annotated[str, typer.Option(help="PR body")] = "",
) -> None:
    """Simulate a webhook end-to-end (clone → generate → push tests → open PR)."""
    load_dotenv()
    from app.models import PRPayload
    from app.pipeline import run_pipeline

    if not clone_url or not repo_full_name or not head_ref:
        console.print("[red]clone-url, repo-full-name, and head-ref are required[/red]")
        raise typer.Exit(1)

    payload = PRPayload(
        scm=scm,  # type: ignore[arg-type]
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        base_ref=base_ref,
        base_sha="",
        head_ref=head_ref,
        head_sha=head_sha or head_ref,
        clone_url=clone_url,
    )
    report = run_pipeline(payload)
    console.print(f"[green]Done.[/green] Tests PR: {report.tests_pr_url or '(none opened)'}")
    console.print(f"Existing suite: {report.existing_suite_summary}")
    if report.suspicious_flags:
        console.print("[red]Suspicious flags:[/red]")
        for f in report.suspicious_flags:
            console.print(f"  - {f['file']}: {f['reasoning']}")


if __name__ == "__main__":
    app()
