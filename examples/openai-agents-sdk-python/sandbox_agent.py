"""An OpenAI Agents SDK agent with a NeevCloud sandbox as its tool.

Uses the chat-completions model wrapper so it works against any OpenAI-compatible
endpoint, not only api.openai.com.
"""

import asyncio
import os
import uuid

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled
from neevai import NeevAI
from openai import AsyncOpenAI

MODEL_BASE_URL = os.environ["NEEV_MODEL_BASE_URL"]
MODEL_API_KEY = os.environ["NEEV_MODEL_API_KEY"]
MODEL_NAME = os.environ.get("NEEV_MODEL", "glm-5-2")

# No OpenAI account is involved, so there is no tracing backend to talk to.
set_tracing_disabled(True)

client = NeevAI()
sandbox = None


@function_tool
def run(command: str) -> str:
    """Run a shell command on a Linux machine. State persists between calls."""
    result = sandbox.exec("sh", args=["-lc", command])
    if result.exit_code != 0:
        return f"exit {result.exit_code}: {result.stderr or result.stdout}"
    return result.stdout or "(no output)"


async def main() -> None:
    """Start a sandbox, run the agent against it, then delete it."""
    global sandbox
    sandbox = client.sandboxes.create({"name": f"oa-{uuid.uuid4().hex[:8]}"})
    sandbox.wait_until_ready()
    print(f"  sandbox ready: {sandbox.name}")

    agent = Agent(
        name="Sandbox engineer",
        instructions="You have a Linux sandbox. Run commands rather than guessing.",
        tools=[run],
        model=OpenAIChatCompletionsModel(
            model=MODEL_NAME,
            openai_client=AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=MODEL_API_KEY),
        ),
    )

    try:
        result = await Runner.run(
            agent,
            "Create a file primes.py that prints the first 10 prime numbers, run it "
            "with python3, and reply with exactly what it printed.",
        )
        print(f"\n  agent: {result.final_output.strip()[:200]}")
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


if __name__ == "__main__":
    asyncio.run(main())
