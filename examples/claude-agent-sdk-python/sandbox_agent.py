"""A Claude Agent SDK agent whose only tools are the NeevCloud sandbox tools.

The tools run in-process as an SDK MCP server. Claude Code's built-in tools act on
the machine running this script, so they are all switched off.
"""

import asyncio
import os
import uuid

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from neevai import NeevAI
from neevai.agent_tools import SandboxTools
from neevai.agent_tools.claude import to_claude

TASK = (
    "Write primes.py that prints the first 10 prime numbers, run it with python3, "
    "and reply with exactly what it printed."
)


async def run_agent(tools: SandboxTools) -> str:
    """Runs TASK; raises the first sandbox exception, a host tool's use, or a run that did not succeed."""
    claude = to_claude(tools)
    options = ClaudeAgentOptions(
        **claude.options(),
        system_prompt="You have a Linux sandbox. Use the tools rather than guessing.",
        max_turns=12,
    )
    final, failed = "", None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(TASK)
        async for msg in claude.watch(client.receive_response()):
            if isinstance(msg, ResultMessage):
                final = msg.result or ""
                if msg.is_error or msg.subtype != "success":
                    failed = msg.subtype
    if failed:
        raise RuntimeError(f"agent run did not succeed: {failed}: {final}")
    return final


async def main() -> None:
    """Start a sandbox, run the agent against it, then delete it."""
    client = NeevAI()
    sandbox = client.sandboxes.create({"name": f"claude-{uuid.uuid4().hex[:8]}"})

    # The sandbox holds quota from here, so the try starts here.
    try:
        sandbox.wait_until_ready()
        print(f"  sandbox ready: {sandbox.name}")
        tools = SandboxTools(
            sandbox,
            allow_preview=os.environ.get("ALLOW_PREVIEW") == "1",
            allow_rollback=os.environ.get("ALLOW_ROLLBACK") == "1",
            allow_processes=os.environ.get("ALLOW_PROCESSES") == "1",
        )
        print(f"\n  agent: {(await run_agent(tools)).strip()[:200]}")
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


if __name__ == "__main__":
    asyncio.run(main())
