"""
Kubernetes Triage Agent

Usage:
    python -m agent.triage --namespace default
    python -m agent.triage --namespace payments --output reports/triage.md
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import (
    query, ClaudeAgentOptions, AssistantMessage,
    TextBlock, ToolUseBlock, ResultMessage,
)
from langfuse import get_client

from agent.prompts import TRIAGE_SYSTEM_PROMPT

langfuse = get_client()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _find_claude_cli() -> str:
    """Locate the claude binary rather than hardcoding a path.

    The auditor hardcodes /usr/local/bin/claude, which is the Intel Homebrew
    prefix. On Apple Silicon brew installs to /opt/homebrew, so a hardcoded
    path breaks the moment the toolchain is fixed or the project moves to
    another machine. shutil.which respects PATH and is correct everywhere.
    """
    cli = shutil.which("claude")
    if not cli:
        print(
            "Error: could not find the 'claude' CLI on PATH.\n"
            "Install it with: brew install --cask claude-code",
            file=sys.stderr,
        )
        sys.exit(1)
    return cli


async def run_triage(namespace: str, output_path: str = None):
    """Diagnose broken workloads in a namespace."""

    options = ClaudeAgentOptions(
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        model="claude-sonnet-4-6",
        # Higher than the auditor's 10. The auditor reads one file and runs
        # three checks over it. Triage investigates an unknown number of pods
        # with a variable number of tool calls each, and running out of turns
        # mid-investigation produces a truncated diagnosis.
        max_turns=20,
        cli_path=_find_claude_cli(),
        allowed_tools=[
            "mcp__k8s-triage__list_pods",
            "mcp__k8s-triage__get_pod_events",
            "mcp__k8s-triage__get_logs",
            "mcp__k8s-triage__describe_deployment",
        ],
        permission_mode="acceptEdits",
        mcp_servers={
            "k8s-triage": {
                "command": sys.executable,
                "args": ["-m", "mcp_server.server"],
                "cwd": str(PROJECT_ROOT),
            }
        },
    )

    prompt = f"Triage the workloads in the '{namespace}' namespace."

    print(f"Triaging namespace: {namespace}")
    print("-" * 60)

    report_lines = []
    tool_calls = []

    # Everything that belongs to this trace stays inside the with block.
    #
    # The auditor gets this wrong: its `async for` loop sits outside the
    # `with`, so the root span opens, assigns an empty list, and closes before
    # a single tool runs. Child spans then attach to nothing and update_trace
    # fires on a closed span, which is why its tags never appeared in the
    # dashboard. Indentation, not the SDK.
    with langfuse.start_as_current_observation(
        as_type="span",
        name="k8s-triage",
        input={"namespace": namespace},
    ) as root:

        async for message in query(prompt=prompt, options=options):

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
                        report_lines.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(block.name)
                        # A span per tool call. On this project the trace is
                        # worth more than it was on the auditor: it shows the
                        # agent choosing which pods to investigate and in what
                        # order. That sequence is the reasoning made visible.
                        with root.start_as_current_observation(
                            as_type="span",
                            name=block.name,
                            input=block.input,
                        ):
                            pass

            if isinstance(message, ResultMessage):
                # Every model billed in this run, not just the first key.
                # Claude Code uses a small fast model for background work
                # alongside the one configured above, so model_usage usually
                # has two entries and [0] reports whichever happens to come
                # first — which is how a Sonnet run gets labelled as Haiku.
                models = (
                    ", ".join(message.model_usage.keys())
                    if message.model_usage else "unknown"
                )
                root.update(output="\n".join(report_lines))
                root.update_trace(
                    metadata={
                        "namespace": namespace,
                        "cost_usd": message.total_cost_usd,
                        "num_turns": message.num_turns,
                        "models": models,
                        "tools_called": tool_calls,
                    },
                    tags=["k8s-triage"],
                )

                print("-" * 60)
                print(f"Triage complete. Cost: ${message.total_cost_usd:.4f}")
                print(f"Models: {models}")
                print(f"Tool calls: {len(tool_calls)}")

    if output_path and report_lines:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text("\n".join(report_lines))
        print(f"Report saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose why Kubernetes workloads are broken."
    )
    parser.add_argument(
        "--namespace", "-n", default="default",
        help="Namespace to triage (default: default)",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Write the report to this path as well as stdout",
    )
    args = parser.parse_args()

    asyncio.run(run_triage(args.namespace, args.output))


if __name__ == "__main__":
    main()