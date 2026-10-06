"""A thin MCP client that connects to the untrusted server over stdio, calls one tool, prints JSON.

It runs inside the quarantine sandbox so the untrusted server is exercised where its egress is
denied, and it treats the server as the agent would: list the tools, call one, keep the answer.
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

SERVER = StdioServerParameters(command=sys.executable, args=["untrusted_server.py"], cwd="/workspace")
SAMPLE_TEXT = "NeevCloud sandboxes run untrusted code with egress you control and a command audit trail."


def _text(result) -> str:
    """Joins an MCP result's text blocks, for a server that returns no structured content."""
    return "\n".join(getattr(block, "text", "") for block in result.content)


def _answer(result):
    """Reads the tool's answer, preferring structured content and falling back to its JSON text block."""
    if result.structured_content is not None:
        return result.structured_content
    raw = _text(result)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


async def _probe() -> dict:
    """Opens an MCP session to the untrusted server, lists its tools, and calls summarize_text once."""
    async with Client(SERVER) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        result = await client.call_tool("summarize_text", {"text": SAMPLE_TEXT})
        return {"ok": not result.is_error, "tools": tools, "answer": _answer(result)}


def main() -> int:
    """Runs the probe and prints its result as one JSON line for the recipe to parse."""
    try:
        report = asyncio.run(_probe())
    except BaseException as exc:  # surface any handshake or client failure as JSON, never a traceback
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
