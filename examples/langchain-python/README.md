# LangChain with NeevCloud Sandboxes

A single LangChain agent whose tools run on a real Linux machine.

The [langgraph-python](../langgraph-python) example connects over MCP. This one
uses the Python SDK and plain `@tool` functions, which is shorter when you are
already writing Python and want to decide exactly what the agent can do.

<p align="center">
  <img src="../../assets/runs/langchain-python.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_BASE_URL=https://inference.ai.neevcloud.com/v1
export NEEV_MODEL_API_KEY=...

python sandbox_agent.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

Output:

```
  sandbox ready: lc-787e0123

  agent: 80.0

  sandbox deleted
```

The agent wrote a CSV, ran Python over it, and reported the mean. It did not
estimate — the number came from code that actually ran.

## The pattern

```python
@tool
def run(command: str) -> str:
    """Run a shell command on a Linux machine. State persists between calls."""
    result = sandbox.exec("sh", args=["-lc", command])
    if result.exit_code != 0:
        return f"exit {result.exit_code}: {result.stderr or result.stdout}"
    return result.stdout or "(no output)"
```

State persists between calls, so the agent can write a file in one step and run
it in the next.
