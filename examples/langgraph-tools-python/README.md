# LangGraph with NeevCloud Sandboxes (tools)

A LangGraph agent, built with langchain's `create_agent`, with the NeevCloud sandbox tools (run_command, write_file, read_file, stat_path, move_path and delete_path, plus opt-in expose_port, checkpoint/rollback and start_process/read_process_output/stop_process/list_processes), built by `to_langchain` from `neevai.agent_tools.langchain`.

For several agents sharing MCP-served sandboxes, see [langgraph-python](../langgraph-python).

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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

## Files

- `sandbox_agent.py` — builds the agent with `create_agent(model, to_langchain(SandboxTools(sandbox)))` and runs one task.

## Contract notes

- A path outside `/workspace`, such as `/etc/passwd` or `../etc/passwd`, comes back as an error result: `HTTP 400 invalid_argument: path "/etc/passwd" escapes workspace root`.
- Absolute paths inside `/workspace` work, and commands run in `/workspace` by default.
- A command still running after `timeout_s` comes back as an error result: `command did not finish within N s`.
- Command output that is not UTF-8 keeps its text, with U+FFFD in place of the bad bytes; a file that is not UTF-8 comes back from `read_file` as a `not UTF-8 text` error result.

## Traps

- A command that prints nothing for about 2 minutes is cut off by the network in front of the sandbox; `run_command` then returns an error result telling the model to use `start_process`. Set `ALLOW_PROCESSES=1` for builds, test suites, servers and other long or quiet work.
- Reasoning models need `max_tokens` headroom or they return no tool call.
- `recursion_limit` bounds the tool loop; raise it for long tasks.
- `create_agent` only turns invalid-argument errors into a message for the model; any other tool exception propagates out of `agent.invoke`, so a bad `NEEV_API_KEY` or a network failure stops the run instead of looping. Bad arguments never raise here: `call()` returns them to the model as an error result. This example relies on that default.
