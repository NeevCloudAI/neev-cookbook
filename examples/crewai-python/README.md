# CrewAI with NeevCloud Sandboxes

CrewAI no longer executes code for you. If you enable `allow_code_execution`
today, the library tells you so itself:

<p align="center">
  <img src="../../assets/runs/crewai-python.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

```
allow_code_execution is deprecated and will be removed in v2.0.
CodeInterpreterTool is no longer available.
```

`CodeInterpreterTool` is already gone from `crewai_tools` — the import fails. So
code execution has to move somewhere, and that somewhere is a sandbox.

This example wires one in as an ordinary CrewAI tool. No fork, no patch, nothing
special: a `BaseTool` subclass that runs commands on a real Linux machine, which
you hand to an agent like any other tool.

## Why a sandbox and not a container of your own

The risk with model-generated code is not a missing container runtime. It is
what that code can reach once it runs. A sandbox here is a machine with root inside and a boundary
outside: egress denies everything by default until you allow a domain, so an
agent talked into exfiltrating data has nowhere to send it.

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_BASE_URL=https://inference.ai.neevcloud.com/v1   # or any OpenAI-compatible endpoint
export NEEV_MODEL_API_KEY=...

python sandboxed_crew.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

Optional: `NEEV_MODEL` (defaults to `glm-5-2`).

Output:

```
  sandbox crew-65950523 ready

  result: 832040

  sandbox deleted
```

The agent wrote a Python file, ran it with `python3`, and reported what it
printed. It did not estimate.

## The tool

The whole integration is one class:

```python
class SandboxTool(BaseTool):
    name: str = "run_in_sandbox"
    description: str = "Run a shell command on a Linux machine..."
    args_schema: type[BaseModel] = RunInput
    sandbox_id: str = ""

    def _run(self, command: str) -> str:
        sandbox = client.sandboxes.get(self.sandbox_id)
        result = sandbox.exec("sh", args=["-lc", command])
        if result.exit_code != 0:
            return f"exit {result.exit_code}: {result.stderr or result.stdout}"
        return result.stdout or "(no output)"
```

Binding the sandbox id to the tool instance means the agent never passes one
around, and cannot reach a sandbox it was not given.

## A machine per agent

Give each agent its own `SandboxTool` with its own sandbox id and the crew is
isolated by construction — one agent cannot read another's files even if it
tries. Each sandbox carries its own egress allow-list, and the audit trail
records commands against the key that ran them, so you can tell which agent did
what.

For the same pattern over MCP instead of the SDK, see
[langgraph-python](../langgraph-python).

## Notes

**State persists between tool calls.** The agent can write a file in one call and
run it in the next. That is the point of a machine rather than a function.

**Delete what you create.** A sandbox consumes quota while it exists. This
example deletes it in a `finally` block. Idle sandboxes also pause themselves and
stop billing compute, but they still hold quota until deleted.

**Environment variables are `NEEV_*`.** Older documentation shows `NEEVCLOUD_*`;
the current release does not read those.
