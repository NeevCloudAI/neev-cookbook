"""A LangGraph agent (langchain create_agent) with the NeevCloud sandbox tools.

For several agents sharing MCP-served sandboxes, see ../langgraph-python.
"""

import os
import uuid

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from neevai import NeevAI
from neevai.agent_tools import SandboxTools
from neevai.agent_tools.langchain import to_langchain

TASK = (
    "Write primes.py that prints the first 10 prime numbers, run it with python3, "
    "and reply with exactly what it printed."
)


def model() -> ChatOpenAI:
    """Agent model. Reasoning models need headroom or they return no tool call."""
    return ChatOpenAI(
        model=os.environ.get("NEEV_MODEL", "glm-5-2"),
        base_url=os.environ["NEEV_MODEL_BASE_URL"],
        api_key=os.environ["NEEV_MODEL_API_KEY"],
        max_tokens=3000,
        temperature=0,
    )


def main() -> None:
    """Start a sandbox, run the agent against it, then delete it."""
    client = NeevAI()
    sandbox = client.sandboxes.create({"name": f"lg-{uuid.uuid4().hex[:8]}"})

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
        agent = create_agent(
            model(), to_langchain(tools), system_prompt="You have a Linux sandbox. Use the tools rather than guessing."
        )
        result = agent.invoke({"messages": [{"role": "user", "content": TASK}]}, {"recursion_limit": 30})
        print(f"\n  agent: {str(result['messages'][-1].content).strip()[:200]}")
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


if __name__ == "__main__":
    main()
