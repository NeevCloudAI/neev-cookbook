"""A LangChain agent with a NeevCloud sandbox as its tools.

The LangGraph example connects over MCP. This one uses the Python SDK and plain
`@tool` functions, which is the shorter path when you already write Python and
want control over exactly what the agent can do.
"""

import os
import uuid

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from neevai import NeevAI

MODEL_BASE_URL = os.environ["NEEV_MODEL_BASE_URL"]
MODEL_API_KEY = os.environ["NEEV_MODEL_API_KEY"]
MODEL_NAME = os.environ.get("NEEV_MODEL", "glm-5-2")

client = NeevAI()
sandbox = None


@tool
def run(command: str) -> str:
    """Run a shell command on a Linux machine. State persists between calls."""
    result = sandbox.exec("sh", args=["-lc", command])
    if result.exit_code != 0:
        return f"exit {result.exit_code}: {result.stderr or result.stdout}"
    return result.stdout or "(no output)"


@tool
def write_file(path: str, content: str) -> str:
    """Write a file in the sandbox."""
    sandbox.files.write(path, content)
    return f"wrote {path}"


def main() -> None:
    """Start a sandbox, let the agent use it, then delete it."""
    global sandbox
    sandbox = client.sandboxes.create({"name": f"lc-{uuid.uuid4().hex[:8]}"})

    # The sandbox holds quota from here, so the try starts here -- not after the
    # readiness wait, which can itself fail.
    try:
        _run_agent()
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


def _run_agent() -> None:
    """Wait for the sandbox, then let the agent work in it."""
    sandbox.wait_until_ready()
    print(f"  sandbox ready: {sandbox.name}")

    agent = create_agent(
        ChatOpenAI(
            model=MODEL_NAME,
            base_url=MODEL_BASE_URL,
            api_key=MODEL_API_KEY,
            max_tokens=3000,
            temperature=0,
        ),
        [run, write_file],
        system_prompt="You have a Linux sandbox. Run code rather than reasoning about it.",
    )

    result = agent.invoke(
        {
            "messages": [
                (
                    "user",
                    "Write a CSV at data.csv with columns name,score and three "
                    "rows. Then use python3 to print the mean score. Reply with "
                    "the mean only.",
                )
            ]
        }
    )
    print(f"\n  agent: {result['messages'][-1].content.strip()[:150]}")


if __name__ == "__main__":
    main()
