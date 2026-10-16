"""A Claude Agent SDK agent working in a NeevCloud sandbox over the sandbox MCP server.

Claude Code connects to the remote MCP URL itself; the `x-sandbox-name` header picks
the sandbox. The host creates and deletes the sandbox over its own MCP connection,
and Claude only gets the working tools in AGENT_TOOLS.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import httpx2
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, ToolUseBlock
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

REGION = os.environ.get("NEEV_REGION", "as-south-1")
MCP_URL = f"https://mcp.sandboxes.{REGION}.ai.neevcloud.com/mcp"

# The server also publishes lifecycle tools such as create_sandbox and delete_sandbox; Claude gets only these.
AGENT_TOOLS = ["exec", "fs_read", "fs_write", "fs_exists", "fs_move", "fs_remove"]
if os.environ.get("ALLOW_PROCESSES") == "1":
    AGENT_TOOLS += ["process_start", "process_get", "process_logs", "process_kill", "process_list"]
ALLOWED = [f"mcp__neev__{n}" for n in AGENT_TOOLS]

TASK = (
    "Write primes.py that prints the first 10 prime numbers, run it with python3, "
    "and reply with exactly what it printed."
)


def headers(api_key: str, sandbox_name: str) -> dict[str, str]:
    """The bearer key resolves org and project; the name picks the sandbox."""
    return {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}


def build_options(api_key: str, sandbox_name: str, published: list[str]) -> ClaudeAgentOptions:
    """Gives Claude the working tools of one sandbox and nothing else."""
    return ClaudeAgentOptions(
        system_prompt="You have a Linux sandbox. Use the tools rather than guessing.",
        tools=[],
        mcp_servers={"neev": {"type": "http", "url": MCP_URL, "headers": headers(api_key, sandbox_name)}},
        strict_mcp_config=True,
        allowed_tools=ALLOWED,
        # Your own Claude Code settings and hooks stay out of the run; login auth is not a setting.
        setting_sources=[],
        # Anything not in allowed_tools is denied rather than left to a permission prompt.
        permission_mode="dontAsk",
        # allowed_tools only auto-approves; this hides the rest of what the server publishes.
        disallowed_tools=[f"mcp__neev__{n}" for n in published if n not in AGENT_TOOLS],
        max_turns=12,
    )


@asynccontextmanager
async def host_session(api_key: str, sandbox_name: str):
    """The host's own MCP connection to the sandbox, for the lifecycle tools Claude never sees."""
    # create_sandbox waits until the sandbox is reachable, so the read timeout is generous.
    async with httpx2.AsyncClient(headers=headers(api_key, sandbox_name), timeout=httpx2.Timeout(30, read=300)) as http:
        async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
            yield client


async def host_call(client, name: str) -> str:
    """Calls a lifecycle tool with no arguments; raises if the server reports an error."""
    result = await client.call_tool(name, {})
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        raise RuntimeError(f"{name}: {text}")
    return text


async def run_agent(options: ClaudeAgentOptions) -> str:
    """Runs TASK; fails if the run did not succeed or Claude used any tool outside ALLOWED."""
    used, final, failed = [], "", None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(TASK)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                used += [b.name for b in msg.content if isinstance(b, ToolUseBlock)]
            elif isinstance(msg, ResultMessage):
                final = msg.result or ""
                if msg.is_error or msg.subtype != "success":
                    failed = msg.subtype
    stray = [n for n in used if n not in ALLOWED]
    if stray:
        raise RuntimeError(f"tools outside the sandbox allow-list were used: {stray}")
    if failed:
        raise RuntimeError(f"agent run did not succeed: {failed}: {final}")
    return final


async def main() -> None:
    """Create a sandbox over MCP, run Claude in it, then delete it."""
    api_key, name = os.environ["NEEV_API_KEY"], f"claude-mcp-{uuid.uuid4().hex[:8]}"
    async with host_session(api_key, name) as host:
        published = [t.name for t in (await host.list_tools()).tools]
        # A create that fails part-way can still leave a sandbox, so the delete covers it too.
        try:
            await host_call(host, "create_sandbox")
            print(f"  sandbox ready: {name}")
            print(f"\n  agent: {(await run_agent(build_options(api_key, name, published))).strip()[:200]}")
        finally:
            await host_call(host, "delete_sandbox")
            print("\n  sandbox deleted")


if __name__ == "__main__":
    asyncio.run(main())
