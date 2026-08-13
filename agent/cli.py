from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from agent.graph import build_graph
from agent.state import ChangedFile, FileWorkItem

app = typer.Typer(add_completion=False, help="LangGraph unit-test generator (CLI mode)")
console = Console()


@app.command()
def generate(
    repo: Annotated[Path, typer.Option(help="Path to the target repository")],
    files: Annotated[
        list[str],
        typer.Option("--file", "-f", help="Repo-relative source file (repeatable)"),
    ],
    validate: Annotated[bool, typer.Option("--validate/--no-validate")] = True,
    max_attempts: Annotated[int, typer.Option(help="Retry cap per file")] = 3,
) -> None:
    """Generate unit tests for the given source files."""
    load_dotenv()

    if not repo.exists():
        console.print(f"[red]Repo not found:[/red] {repo}")
        raise typer.Exit(1)

    seed_items: list[FileWorkItem] = [
        FileWorkItem(
            file=ChangedFile(
                path=f,
                language="unknown",
                tier="tier2",
                content="",
                existing_tests=None,
            )
        )
        for f in files
    ]

    graph = build_graph()
    final_state = graph.invoke(
        {
            "repo_path": str(repo.resolve()),
            "validate": validate,
            "max_attempts": max_attempts,
            "work_items": seed_items,
            "current_index": 0,
            "errors": [],
        },
        config={"recursion_limit": 100},
    )

    table = Table(title="Test generation results")
    table.add_column("File")
    table.add_column("Language")
    table.add_column("Attempts")
    table.add_column("Status")
    table.add_column("Test path")
    for item in final_state["work_items"]:
        table.add_row(
            item["file"]["path"],
            item["file"]["language"],
            str(item.get("attempts", 0)),
            item.get("status", "?"),
            item.get("test_path", ""),
        )
    console.print(table)

    if final_state.get("errors"):
        console.print("[yellow]Warnings:[/yellow]")
        for err in final_state["errors"]:
            console.print(f"  - {err}")

    console.print(
        f"\n[green]Tests written to:[/green] {repo}/generated_tests/"
    )


if __name__ == "__main__":
    app()
