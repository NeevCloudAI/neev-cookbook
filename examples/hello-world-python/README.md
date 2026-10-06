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
sandbox 01a1103d-833f-7458-9d79-538a9d198c7b is Ready
Linux x86_64
hello from inside
deleted
```

Start here, then see [crewai-python](../crewai-python) for handing a sandbox to
an agent as a tool, or [langgraph-python](../langgraph-python) for connecting
over MCP with no SDK at all.
