"""Command-line conversational interface for the MLOps Copilot agent.

    python -m agent.main                         # interactive chat
    python -m agent.main ask "question"          # single question
    python -m agent.main ask "..." --planner rules --json
    python -m agent.main tools                   # list tools and permissions

State-changing tools always ask for confirmation on the terminal. ``--yes`` pre-approves them for
non-interactive use (the flag itself is the explicit user confirmation).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from rich.console import Console  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.prompt import Confirm  # noqa: E402
from rich.table import Table  # noqa: E402

from agent.config import Settings  # noqa: E402
from agent.orchestrator import MLOpsAgent  # noqa: E402
from agent.schemas import AgentResponse  # noqa: E402
from agent.tools.base import ToolSpec  # noqa: E402

console = Console()


def make_confirm(auto_yes: bool):
    def confirm(spec: ToolSpec, args: dict[str, Any]) -> bool:
        console.print(f"\n[bold yellow]Confirmation required[/] - [bold]{spec.name}[/] changes system state.")
        console.print(f"Arguments: {json.dumps(args)}")
        if auto_yes:
            console.print("[green]Pre-approved with --yes[/]")
            return True
        if not sys.stdin.isatty():
            console.print("[red]No interactive terminal; operation rejected (use --yes to pre-approve).[/]")
            return False
        return Confirm.ask("Proceed?", default=False)

    return confirm


def render(resp: AgentResponse, show_evidence: bool = True) -> None:
    console.print(Markdown(resp.answer))
    if show_evidence and resp.tool_results:
        table = Table(title=f"Tool evidence - request {resp.request_id} - planner {resp.planner}", show_lines=False)
        for col in ("#", "tool", "status", "ms", "error"):
            table.add_column(col)
        for i, r in enumerate(resp.tool_results, 1):
            colour = "green" if r.ok else "red"
            table.add_row(str(i), r.tool, f"[{colour}]{r.status.value}[/]", f"{r.duration_ms:.0f}",
                          (r.error or "")[:80])
        console.print(table)


def write_transcript(path: Path, resp: AgentResponse) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"## {resp.question}\n\n")
        fh.write(f"*Request `{resp.request_id}` - planner `{resp.planner}` - "
                 f"{datetime.now(tz=UTC).isoformat(timespec='seconds')}*\n\n")
        fh.write(resp.answer + "\n\n")
        fh.write("| # | tool | status | duration (ms) | error |\n|---|---|---|---|---|\n")
        for i, r in enumerate(resp.tool_results, 1):
            fh.write(f"| {i} | `{r.tool}` | {r.status.value} | {r.duration_ms:.0f} | {(r.error or '')[:120]} |\n")
        fh.write("\n<details><summary>Raw tool outputs</summary>\n\n```json\n")
        fh.write(json.dumps([r.model_dump(mode="json") for r in resp.tool_results], indent=2, default=str)[:60000])
        fh.write("\n```\n</details>\n\n---\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mlops-agent", description="MLOps Copilot agent")
    parser.add_argument("--planner", choices=["auto", "gemini", "rules"], default="auto")
    parser.add_argument("--yes", action="store_true", help="pre-approve state-changing operations")
    parser.add_argument("--json", action="store_true", help="print the full AgentResponse as JSON")
    parser.add_argument("--no-evidence", action="store_true", help="hide the tool evidence table")
    parser.add_argument("--transcript", type=Path, help="append the conversation to a Markdown file")
    sub = parser.add_subparsers(dest="command")
    ask = sub.add_parser("ask", help="ask a single question")
    ask.add_argument("question", nargs="+")
    sub.add_parser("tools", help="list available tools")
    sub.add_parser("chat", help="interactive chat (default)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    agent = MLOpsAgent(settings, planner=args.planner, confirm=make_confirm(args.yes))

    if args.command == "tools":
        table = Table(title=f"{len(agent.registry)} tools")
        for col in ("module", "tool", "state-changing", "description"):
            table.add_column(col)
        for t in sorted(agent.tool_catalog(), key=lambda t: (t["module"], t["name"])):
            table.add_row(t["module"], t["name"], "yes" if t["mutating"] else "no", t["description"][:90])
        console.print(table)
        return 0

    def handle(question: str) -> None:
        with console.status("Working..."):
            resp = agent.ask(question)
        if args.json:
            print(resp.model_dump_json(indent=2))
        else:
            render(resp, show_evidence=not args.no_evidence)
        if args.transcript:
            write_transcript(args.transcript, resp)

    if args.command == "ask":
        handle(" ".join(args.question))
        return 0

    planner = agent.llm.model if agent.llm and args.planner != "rules" else "rule-based"
    console.print(f"[bold]MLOps Copilot[/] (planner: {planner}). Type 'exit' to quit, 'reset' to clear memory.")
    while True:
        try:
            question = console.input("\n[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in {"exit", "quit"}:
            break
        if question.lower() == "reset":
            agent.reset()
            continue
        if question:
            handle(question)
    return 0


if __name__ == "__main__":
    sys.exit(main())
