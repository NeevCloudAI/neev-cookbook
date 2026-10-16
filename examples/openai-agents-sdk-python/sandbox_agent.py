"""An OpenAI Agents SDK agent with the NeevCloud sandbox tools.

Uses the chat-completions model wrapper so it works against any OpenAI-compatible
endpoint, not only api.openai.com.
"""

import asyncio
import os
import uuid

from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from neevai import NeevAI
from neevai.agent_tools import SandboxTools
from neevai.agent_tools.openai_agents import to_openai_agents
from openai import AsyncOpenAI

MODEL_BASE_URL = os.environ["NEEV_MODEL_BASE_URL"]
MODEL_API_KEY = os.environ["NEEV_MODEL_API_KEY"]
MODEL_NAME = os.environ.get("NEEV_MODEL", "glm-5-2")

TASK = (
    "Write primes.py that prints the first 10 prime numbers, run it with python3, "
    "and reply with exactly what it printed."
)

# No OpenAI account is involved, so there is no tracing backend to talk to.
set_tracing_disabled(True)


async def main() -> None:
    """Start a sandbox, run the agent against it, then delete it."""
    client = NeevAI()
    sandbox = client.sandboxes.create({"name": f"oa-{uuid.uuid4().hex[:8]}"})

    # The sandbox holds quota from here, so the try starts here -- the readiness
    # wait can fail too, and that must not leak it.
    try:
        sandbox.wait_until_ready()
        print(f"  sandbox ready: {sandbox.name}")
        tools = SandboxTools(
            sandbox,
            allow_preview=os.environ.get("ALLOW_PREVIEW") == "1",
            allow_rollback=os.environ.get("ALLOW_ROLLBACK") == "1",
            allow_processes=os.environ.get("ALLOW_PROCESSES") == "1",
        )
        agent = Agent(
            name="Sandbox engineer",
            instructions="You have a Linux sandbox. Use the tools rather than guessing.",
            tools=to_openai_agents(tools),
            model=OpenAIChatCompletionsModel(
                model=MODEL_NAME,
                openai_client=AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=MODEL_API_KEY),
            ),
        )
        result = await Runner.run(agent, TASK, max_turns=12)
        print(f"\n  agent: {str(result.final_output or '').strip()[:200]}")
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


if __name__ == "__main__":
    asyncio.run(main())
