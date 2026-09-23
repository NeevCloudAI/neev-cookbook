"""CrewAI agents that run code in a NeevCloud sandbox.

CrewAI removed CodeInterpreterTool and deprecated allow_code_execution, so code
execution now belongs in a dedicated sandbox. This wires one in as an ordinary
CrewAI tool: the agent writes a script, runs it, and reads the output back.
"""

import os
import uuid

from crewai import LLM, Agent, Crew, Task
from crewai.tools import BaseTool
from neevai import NeevAI
from pydantic import BaseModel, Field

MODEL_BASE_URL = os.environ["NEEV_MODEL_BASE_URL"]
MODEL_API_KEY = os.environ["NEEV_MODEL_API_KEY"]
MODEL_NAME = os.environ.get("NEEV_MODEL", "glm-5-2")

client = NeevAI()


class RunInput(BaseModel):
    """Arguments the agent supplies when it calls the tool."""

    command: str = Field(description="Shell command to run inside the sandbox")


class SandboxTool(BaseTool):
    """Runs a shell command in one sandbox and returns its output."""

    name: str = "run_in_sandbox"
    description: str = (
        "Run a shell command on a Linux machine. Use it to write files, install "
        "packages and execute code. State persists between calls."
    )
    args_schema: type[BaseModel] = RunInput
    sandbox_id: str = ""

    def _run(self, command: str) -> str:
        """Execute the command and return stdout, or stderr when it fails."""
        sandbox = client.sandboxes.get(self.sandbox_id)
        result = sandbox.exec("sh", args=["-lc", command])
        if result.exit_code != 0:
            return f"exit {result.exit_code}: {result.stderr or result.stdout}"
        return result.stdout or "(no output)"


def start_sandbox(name: str):
    """Create a sandbox and wait until it can run commands."""
    sandbox = client.sandboxes.create({"name": name})
    sandbox.wait_until_ready()
    print(f"  sandbox {name} ready")
    return sandbox


def main() -> None:
    """Give one agent a sandbox, ask it to compute something, print the answer."""
    sandbox = start_sandbox(f"crew-{uuid.uuid4().hex[:8]}")

    llm = LLM(
        model=f"openai/{MODEL_NAME}",
        base_url=MODEL_BASE_URL,
        api_key=MODEL_API_KEY,
        max_tokens=3000,
    )

    analyst = Agent(
        role="Data analyst",
        goal="Answer questions by writing and running Python",
        backstory="You never estimate. You write a script, run it, and report what it printed.",
        tools=[SandboxTool(sandbox_id=sandbox.id)],
        llm=llm,
        verbose=False,
    )

    task = Task(
        description=(
            "Write a Python script to a file that computes the 30th Fibonacci "
            "number, run it with python3, and report the number it printed."
        ),
        expected_output="The 30th Fibonacci number.",
        agent=analyst,
    )

    try:
        result = Crew(agents=[analyst], tasks=[task], verbose=False).kickoff()
        print(f"\n  result: {str(result).strip()[:200]}")
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


if __name__ == "__main__":
    main()
