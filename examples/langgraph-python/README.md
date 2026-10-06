# LangGraph with NeevCloud Sandboxes

Give every agent in a LangGraph crew its own Linux machine, over a single MCP URL.

There is nothing to install and no bridge process. Point `langchain-mcp-adapters`
at the sandbox MCP endpoint with your API key, and the agent gets the whole
surface: create a sandbox, run commands, read and write files, manage processes,
expose ports, snapshot and roll back.

## What this example shows

Two agents run in sequence. Each one holds a connection bound to its own sandbox
by the `x-sandbox-name` header, so neither can touch the other's filesystem — not
by policy, but because the connection only reaches one machine.

The writer is deliberately asked to go looking for a file the researcher wrote.
It does not find it, and the script confirms that directly at the end rather than
taking the model's word for it.

## Run it

```bash
pip install -r requirements.txt

export NEEV_API_KEY=...            # from the NeevCloud console
export NEEV_MODEL_BASE_URL=https://inference.ai.neevcloud.com/v1   # or any OpenAI-compatible endpoint
export NEEV_MODEL_API_KEY=...

python multi_agent_sandboxes.py
```

Optional: `NEEV_REGION` (defaults to `as-south-1`) and `NEEV_MODEL` (defaults to `glm-4-7`).

Expected output:

```
two agents, two sandboxes, one MCP URL

  created crew-researcher
  created crew-writer
  researcher: Linux x86_64
  writer: NOT FOUND

  researcher reads its own note : True
  writer reads the same note    : False

  isolation: ISOLATED

  deleted crew-researcher
  deleted crew-writer
```

## Connecting

The whole integration is a URL and two headers:

```python
MultiServerMCPClient({
    "neev": {
        "transport": "streamable_http",
        "url": "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp",
        "headers": {
            "Authorization": f"Bearer {NEEV_API_KEY}",
            "x-sandbox-name": "my-agent",
        },
    }
})
```

The bearer token identifies you and resolves your organization and project. The
sandbox name picks which machine this connection works in — give two agents the
same name and they share a filesystem, give them different names and they don't.

## Notes

**Give each agent only the tools it needs.** The connection publishes 22 tools.
Loading all of them into every agent costs context and invites wrong tool choices.
Filter the list from `get_tools()` per agent, as this example does.

**Load tools once.** `get_tools()` performs a round trip. Call it per sandbox at
startup and reuse the result rather than on every node entry.

**Reasoning models need token headroom.** A model that reasons before answering
can spend its entire budget thinking and return an empty message with no tool
call. Give it room — this example uses 3000.

**Delete what you create.** A sandbox consumes quota for as long as it exists.
This example deletes both in a `finally` block. Sandboxes also pause themselves
when idle, so a forgotten one stops costing compute, but it still holds quota.
