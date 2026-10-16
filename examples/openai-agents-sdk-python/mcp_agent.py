"""An OpenAI Agents SDK agent working in a NeevCloud sandbox over the sandbox MCP server.

No tool glue: the SDK connects to the MCP URL, and the `x-sandbox-name` header picks
the sandbox. The host creates and deletes the sandbox; the agent only sees the working
tools in AGENT_TOOLS.
"""

import asyncio
import os
import uuid

from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from agents.mcp import MCPServerStreamableHttp, create_static_tool_filter
from openai import AsyncOpenAI

REGION = os.environ.get("NEEV_REGION", "as-south-1")
MCP_URL = f"https://mcp.sandboxes.{REGION}.ai.neevcloud.com/mcp"

MODEL_BASE_URL = os.environ.get("NEEV_MODEL_BASE_URL", "https://inference.ai.neevcloud.com/v1")
MODEL_NAME = os.environ.get("NEEV_MODEL", "glm-5-2")

# The server also publishes lifecycle tools such as create_sandbox and delete_sandbox; the agent gets only these.
AGENT_TOOLS = ["exec", "fs_read", "fs_write", "fs_exists", "fs_move", "fs_remove"]
if os.environ.get("ALLOW_PROCESSES") == "1":
    AGENT_TOOLS += ["process_start", "process_get", "process_logs", "process_kill", "process_list"]

TASK = (
    "Write primes.py that prints the first 10 prime numbers, run it with python3, "
    "and reply with exactly what it printed."
)

# No OpenAI account is involved, so there is no tracing backend to talk to.
set_tracing_disabled(True)


def sandbox_server(api_key: str, sandbox_name: str) -> MCPServerStreamableHttp:
    """MCP connection bound to one sandbox, exposing only AGENT_TOOLS to the agent."""
    return MCPServerStreamableHttp(
        params={
            "url": MCP_URL,
            "headers": {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name},
            "timeout": 30,
        },
        name="neev",
        # create_sandbox waits until the sandbox is reachable, and exec runs to completion.
        client_session_timeout_seconds=300,
        cache_tools_list=True,
        tool_filter=create_static_tool_filter(allowed_tool_names=AGENT_TOOLS),
    )


def tool_text(result) -> str:
    """Text of an MCP tool result."""
    return "".join(getattr(c, "text", "") for c in result.content)


async def host_call(server: MCPServerStreamableHttp, name: str) -> None:
    """Calls a lifecycle tool the agent never sees; raises if the server reports an error."""
    result = await server.call_tool(name, {})
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        raise RuntimeError(f"{name}: {tool_text(result)}")


async def main() -> None:
    """Create a sandbox over MCP, run the agent in it, then delete it."""
    name = f"oa-mcp-{uuid.uuid4().hex[:8]}"
    async with sandbox_server(os.environ["NEEV_API_KEY"], name) as server:
        # A create that fails part-way can still leave a sandbox, so the delete covers it too.
        try:
            # The tool filter applies to what the agent lists, not to the host's own calls.
            await host_call(server, "create_sandbox")
            print(f"  sandbox ready: {name}")
            agent = Agent(
                name="Sandbox engineer",
                instructions="You have a Linux sandbox. Use the tools rather than guessing.",
                mcp_servers=[server],
                model=OpenAIChatCompletionsModel(
                    model=MODEL_NAME,
                    openai_client=AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"]),
                ),
            )
            result = await Runner.run(agent, TASK, max_turns=12)
            print(f"\n  agent: {str(result.final_output or '').strip()[:200]}")
        finally:
            await host_call(server, "delete_sandbox")
            print("\n  sandbox deleted")


if __name__ == "__main__":
    asyncio.run(main())
