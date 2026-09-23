# Hello World

The smallest useful thing: create a sandbox, run a command, delete it.

```bash
pip install -r requirements.txt

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...

python hello_world.py
```

Output:

```
sandbox 01a0ce3a-72d4-7c8d-a80e-5809d8ea9a68 is Ready
Linux 4.19.0-gvisor
hello from inside
deleted
```

Optional: `NEEV_REGION` (defaults to `as-south-1`).

Start here, then see [crewai-python](../crewai-python) for handing a sandbox to
an agent as a tool, or [langgraph-python](../langgraph-python) for connecting
over MCP with no SDK at all.
