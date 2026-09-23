"""Two LangGraph agents, each working in its own NeevCloud sandbox.

Both connect to the same MCP URL with a different `x-sandbox-name`. That header is
the whole binding, so an agent cannot reach another agent's sandbox. The run ends
by checking it: the writer looks for the researcher's file and does not find it.
"""

import asyncio
import os

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

REGION = os.environ.get("NEEV_REGION", "as-south-1")
MCP_URL = f"https://mcp.sandboxes.{REGION}.ai.neevcloud.com/mcp"
NEEV_API_KEY = os.environ["NEEV_API_KEY"]

MODEL_BASE_URL = os.environ["NEEV_MODEL_BASE_URL"]
MODEL_API_KEY = os.environ["NEEV_MODEL_API_KEY"]
MODEL_NAME = os.environ.get("NEEV_MODEL", "glm-5-2")

# One sandbox per agent. Reuse a name to give two agents a shared filesystem.
RESEARCHER_SANDBOX = "crew-researcher"
WRITER_SANDBOX = "crew-writer"

# The connection publishes 22 tools. Give each agent only what its job needs.
AGENT_TOOLS = {"exec", "fs_read", "fs_write"}

MARKER = "researcher-private-note"
SYSTEM_PROMPT = "You work inside a Linux sandbox. Use the tools; do not guess."

# Loaded once per sandbox; the catalogue does not change during a run.
TOOLS: dict[str, dict] = {}

# Sandboxes this run is responsible for deleting, recorded as each one opens so a
# failure part-way through still cleans up the ones that already exist.
OPENED: list[str] = []


def model() -> ChatOpenAI:
    """Agent model. Reasoning models need headroom or they return no tool call."""
    return ChatOpenAI(
        model=MODEL_NAME,
        base_url=MODEL_BASE_URL,
        api_key=MODEL_API_KEY,
        max_tokens=3000,
        temperature=0,
    )


def sandbox_client(sandbox_name: str) -> MultiServerMCPClient:
    """MCP connection bound to one sandbox. A URL and a key is the whole setup."""
    return MultiServerMCPClient(
        {
            "neev": {
                "transport": "streamable_http",
                "url": MCP_URL,
                "headers": {
                    "Authorization": f"Bearer {NEEV_API_KEY}",
                    "x-sandbox-name": sandbox_name,
                },
            }
        }
    )


async def open_sandbox(sandbox_name: str) -> None:
    """Load the sandbox's tools, creating the sandbox if it does not exist yet."""
    tools = await sandbox_client(sandbox_name).get_tools()
    TOOLS[sandbox_name] = {t.name: t for t in tools}

    # create_sandbox with no arguments creates the one this connection names.
    if "no sandbox is bound" in str(await TOOLS[sandbox_name]["get_sandbox"].ainvoke({})):
        await TOOLS[sandbox_name]["create_sandbox"].ainvoke({})
        OPENED.append(sandbox_name)
        print(f"  created {sandbox_name}")
    else:
        OPENED.append(sandbox_name)
        print(f"  reusing {sandbox_name}")


def agent_for(sandbox_name: str):
    """Agent holding only this sandbox's working tools."""
    tools = [t for name, t in TOOLS[sandbox_name].items() if name in AGENT_TOOLS]
    return create_agent(model(), tools, system_prompt=SYSTEM_PROMPT)


class CrewState(TypedDict):
    """What the researcher hands to the writer."""

    findings: str
    report: str


async def researcher(state: CrewState) -> dict:
    """Works in its own sandbox and leaves a private note behind."""
    result = await agent_for(RESEARCHER_SANDBOX).ainvoke(
        {
            "messages": [
                (
                    "user",
                    f"Write a file notes.txt containing exactly '{MARKER}'. "
                    "Then report the kernel version from uname -sr. "
                    "Reply with the kernel string only.",
                )
            ]
        }
    )
    answer = result["messages"][-1].content.strip()
    print(f"  researcher: {answer[:70]}")
    return {"findings": answer}


async def writer(state: CrewState) -> dict:
    """Different sandbox. Asked to go looking for the researcher's note."""
    result = await agent_for(WRITER_SANDBOX).ainvoke(
        {
            "messages": [
                (
                    "user",
                    "Try to read a file called notes.txt. If it is not there, reply "
                    "exactly NOT FOUND. Otherwise reply with its contents.",
                )
            ]
        }
    )
    answer = result["messages"][-1].content.strip()
    print(f"  writer: {answer[:70]}")
    return {"report": answer}


async def check_isolation() -> str:
    """Check the boundary directly rather than trusting what the models did."""
    read_note = {"program": "sh", "args": ["-lc", "cat notes.txt 2>&1"]}
    own = str(await TOOLS[RESEARCHER_SANDBOX]["exec"].ainvoke(read_note))
    other = str(await TOOLS[WRITER_SANDBOX]["exec"].ainvoke(read_note))

    print(f"\n  researcher reads its own note : {MARKER in own}")
    print(f"  writer reads the same note    : {MARKER in other}")

    if MARKER not in own:
        return "INCONCLUSIVE - the researcher never wrote the note"
    return "ISOLATED" if MARKER not in other else "LEAKED"


async def cleanup() -> None:
    """Delete every sandbox this run opened, even if some step failed.

    One failed delete must not skip the rest, so each is reported and the loop
    continues -- a sandbox left behind keeps costing quota.
    """
    for name in OPENED:
        try:
            await TOOLS[name]["delete_sandbox"].ainvoke({})
            print(f"  deleted {name}")
        except Exception as exc:
            print(f"  could not delete {name}: {exc}")


async def main() -> None:
    """Open both sandboxes, run researcher then writer, check the boundary."""
    print("two agents, two sandboxes, one MCP URL\n")

    # Opening is inside the try as well: if the second sandbox fails to open, the
    # first one already exists and still has to be deleted.
    try:
        await open_sandbox(RESEARCHER_SANDBOX)
        await open_sandbox(WRITER_SANDBOX)
        await run_crew()
    finally:
        print()
        await cleanup()


async def run_crew() -> None:
    """Run researcher -> writer, then verify isolation deterministically."""
    graph = StateGraph(CrewState)
    graph.add_node("researcher", researcher)
    graph.add_node("writer", writer)
    graph.add_edge(START, "researcher")
    graph.add_edge("researcher", "writer")
    graph.add_edge("writer", END)
    crew = graph.compile()

    await crew.ainvoke({"findings": "", "report": ""})
    print(f"\n  isolation: {await check_isolation()}")


if __name__ == "__main__":
    asyncio.run(main())
