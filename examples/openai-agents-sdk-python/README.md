# OpenAI Agents SDK with NeevCloud Sandboxes

An Agents SDK agent with the NeevCloud sandbox tools (run_command, write_file, read_file, stat_path, move_path and delete_path, plus opt-in expose_port, checkpoint/rollback and start_process/read_process_output/stop_process/list_processes), built by `to_openai_agents` from `neevai.agent_tools.openai_agents`.

`OpenAIChatCompletionsModel` is used rather than the default Responses API, so
this works against any OpenAI-compatible endpoint — including NeevCloud's own
inference API, which is what the commands below point at.

<p align="center">
  <img src="../../assets/runs/openai-agents-sdk-python.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt  # neevai[openai-agents] brings the Agents SDK

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_BASE_URL=https://inference.ai.neevcloud.com/v1
export NEEV_MODEL_API_KEY=...
# export ALLOW_PREVIEW=1
# export ALLOW_ROLLBACK=1
# export ALLOW_PROCESSES=1

python sandbox_agent.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The agent writes `primes.py`, runs it, and reports the output.

Output:

````
  sandbox ready: oa-92008635

  agent: Here is exactly what it printed:

```
2
3
5
7
11
13
17
19
23
29
```

  sandbox deleted
````

## Use the MCP server instead

`mcp_agent.py` runs the same task with no tool glue: the SDK's `MCPServerStreamableHttp` connects to the NeevCloud sandbox MCP server, the `x-sandbox-name` header picks the sandbox, and a static tool filter gives the agent only `exec`, `fs_read`, `fs_write`, `fs_exists`, `fs_move` and `fs_remove` (`ALLOW_PROCESSES=1` adds `process_start`, `process_get`, `process_logs`, `process_kill` and `process_list`). The script creates and deletes the sandbox itself over the same connection. Prefer it when you want no glue code, the same tools Claude Code and Cursor use, and calls that wake a paused sandbox; the trade-off is that the server decides output size and error text. It needs no organization or project IDs, and `NEEV_REGION` defaults to `as-south-1`.

```bash
python mcp_agent.py
```

## Files

- `sandbox_agent.py` — gives the agent `to_openai_agents(SandboxTools(sandbox))` as its tools and runs one task.
- `mcp_agent.py` — the same task over the sandbox MCP server, with no tool glue.

## Contract notes

- A path outside `/workspace`, such as `/etc/passwd` or `../etc/passwd`, comes back as an error result: `HTTP 400 invalid_argument: path "/etc/passwd" escapes workspace root`.
- Absolute paths inside `/workspace` work, and commands run in `/workspace` by default.
- A command still running after `timeout_s` comes back as an error result: `command did not finish within N s`.
- Command output that is not UTF-8 keeps its text, with U+FFFD in place of the bad bytes; a file that is not UTF-8 comes back from `read_file` as a `not UTF-8 text` error result.

## Traps

- A command that prints nothing for about 2 minutes is cut off by the network in front of the sandbox; `run_command` then returns an error result telling the model to use `start_process`. Set `ALLOW_PROCESSES=1` for builds, test suites, servers and other long or quiet work.
- Without `set_tracing_disabled(True)` the SDK tries to upload traces to OpenAI and logs errors. Remove it only if you use api.openai.com and want traces.
- The default Responses API model only works against OpenAI; `OpenAIChatCompletionsModel` works against any OpenAI-compatible endpoint.
- `function_tool` turns every tool exception into a message for the model by default, which hides auth and network failures behind a loop that runs to `max_turns`. The tools `to_openai_agents` builds stop the run instead, as a `UserError` whose `__cause__` is the original error, and return bad arguments to the model as an error result.
