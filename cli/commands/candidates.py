"""Candidate skill management commands."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.skill.provider import SkillProvider

console = Console()


async def _create_provider() -> SkillProvider:
    return await SkillProvider.create_default()


def candidates_list_command() -> None:
    """List candidate skill revisions."""

    async def _run() -> None:
        provider = await _create_provider()
        rows = provider.list_candidates()
        if not rows:
            console.print("[dim]No candidate skills found.[/dim]")
            return

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Skill", style="green")
        table.add_column("Created", style="yellow")
        table.add_column("Status", style="magenta")
        table.add_column("Path", style="dim")
        table.add_column("Summary", style="white")

        for row in rows:
            summary = row.get("patch_summary") or row.get("rejection_reason") or ""
            if len(summary) > 80:
                summary = summary[:77] + "..."
            table.add_row(
                str(row.get("skill_name", "")),
                str(row.get("created_at", "")),
                str(row.get("status", "")),
                str(row.get("path", "")),
                summary,
            )

        console.print(table)

    asyncio.run(_run())


def candidates_promote_command(candidate_path: str) -> None:
    """Promote a candidate skill revision."""

    async def _run() -> None:
        provider = await _create_provider()
        result = await provider.promote_candidate(candidate_path=candidate_path)
        console.print(
            f"[green]Promoted[/green] {result.get('skill_name')} -> {result.get('path')} (v{result.get('version')})"
        )

    asyncio.run(_run())


def candidates_reject_command(candidate_path: str, reason: str) -> None:
    """Reject a candidate skill revision."""

    async def _run() -> None:
        provider = await _create_provider()
        result = await provider.reject_candidate(
            candidate_path=candidate_path,
            reason=reason,
        )
        console.print(
            f"[yellow]Rejected[/yellow] {result.get('path')} ({result.get('reason')})"
        )

    asyncio.run(_run())


def candidates_show_command(candidate_path: str) -> None:
    """Show candidate metadata and current SKILL.md."""

    async def _run() -> None:
        provider = await _create_provider()
        details = provider.get_candidate_details(candidate_path)
        metadata = details.get("metadata", {}) or {}

        console.print(
            Panel(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                title="Candidate Metadata",
                border_style="cyan",
            )
        )
        console.print(
            Syntax(
                str(details.get("skill_md", "")),
                "markdown",
                theme="ansi_dark",
                line_numbers=True,
            )
        )

    asyncio.run(_run())


def candidates_diff_command(candidate_path: str) -> None:
    """Show unified diff between live and candidate SKILL.md."""

    async def _run() -> None:
        provider = await _create_provider()
        diff_result = provider.diff_candidate(candidate_path)
        diff_text = str(diff_result.get("diff", ""))
        if not diff_text.strip():
            console.print("[dim]No diff found.[/dim]")
            return
        console.print(
            Panel(
                f"{diff_result.get('skill_name')}\\n{diff_result.get('live_path')}\\n{diff_result.get('candidate_path')}",
                title="Candidate Diff",
                border_style="blue",
            )
        )
        console.print(
            Syntax(
                diff_text,
                "diff",
                theme="ansi_dark",
                line_numbers=False,
            )
        )

    asyncio.run(_run())
