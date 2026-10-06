# Hello World

The smallest useful thing: create a sandbox, run a command, delete it.

<p align="center">
  <img src="../../assets/runs/hello-world-python.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

You need Python 3.11+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...

python hello_world.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

Output:

```
sandbox 01a111e0-da7d-7e3b-894f-5d175ed2f082 is Ready
Linux x86_64
hello from inside
deleted
```

Start here, then see [crewai-python](../crewai-python) for handing a sandbox to
an agent as a tool, or [langgraph-python](../langgraph-python) for connecting
over MCP with no SDK at all.
