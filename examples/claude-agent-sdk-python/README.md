# Claude Agent SDK with NeevCloud Sandboxes

A Claude Agent SDK agent whose only tools are the NeevCloud sandbox tools (run_command, write_file, read_file, stat_path, move_path and delete_path, plus opt-in expose_port, checkpoint/rollback and start_process/read_process_output/stop_process/list_processes).

`to_claude` from `neevai.agent_tools.claude` serves the tools in-process as an SDK
MCP server and switches Claude Code's built-in tools (Bash, Read, Write and the
rest) off, because they would act on the machine running this script rather than
the sandbox.

## Run it

You need Python 3.11+, an [Anthropic API key](https://console.anthropic.com/), a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=...
export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
# export ALLOW_PREVIEW=1
# export ALLOW_ROLLBACK=1
# export ALLOW_PROCESSES=1

python sandbox_agent.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The agent writes `primes.py`, runs it, and reports the output.

## Use the MCP server instead

`mcp_agent.py` points Claude at the NeevCloud sandbox MCP server as a remote `http` server, with the `x-sandbox-name` header picking the sandbox. `tools=[]` switches the built-ins off, `allowed_tools` auto-approves the working tools as `mcp__neev__<tool>`: `exec`, `fs_read`, `fs_write`, `fs_exists`, `fs_move` and `fs_remove` (`ALLOW_PROCESSES=1` adds `process_start`, `process_get`, `process_logs`, `process_kill` and `process_list`), and `disallowed_tools` hides the other tools the server publishes; the script creates and deletes the sandbox over its own MCP connection with the `mcp` Python SDK. Prefer it when you want no glue code, the same tools Claude Code and Cursor use, and calls that wake a paused sandbox; the trade-off is that the server decides output size and error text. It needs no organization or project IDs, and `NEEV_REGION` defaults to `as-south-1`.

```bash
python mcp_agent.py
```

## Files

- `sandbox_agent.py` — passes `to_claude(SandboxTools(sandbox)).options()` to `ClaudeAgentOptions`, reads the run through `watch`, and runs one task.
- `mcp_agent.py` — the same task over the remote sandbox MCP server, with no tool glue.

## Traps

- A command that prints nothing for about 2 minutes is cut off by the network in front of the sandbox; `run_command` then returns an error result telling the model to use `start_process`. Set `ALLOW_PROCESSES=1` for builds, test suites, servers and other long or quiet work.
- Tool names reach Claude as `mcp__neev__<tool>`; `options()` already lists them in `allowed_tools`.
- `allowed_tools` only auto-approves; it does not hide a tool. `permission_mode="dontAsk"` denies anything not pre-approved, and against the remote server `mcp_agent.py` also lists everything else the server publishes in `disallowed_tools` so Claude never sees it.
- By default the Claude Agent SDK loads your `~/.claude` and project settings, including hooks that would run on your machine with sandbox output. `options()` sets `setting_sources=[]` to keep them out; a Claude Code login still authenticates.
- Claude Code's built-in tools act on your machine, not the sandbox. `options()` sets `tools=[]`, which switches all of them off; a `disallowed_tools` list would have to keep up with every new built-in.
- The Claude Agent SDK turns every exception a tool raises into an error result for Claude, so a bad `NEEV_API_KEY` or a network failure would loop until `max_turns` and the run would still end in success. `to_claude` answers Claude with a fixed message instead (SDK errors can carry request URLs with org and project IDs), and `watch` stops at the next message and raises the exception. A run that ends without success, such as `error_max_turns`, raises too. Bad arguments are rejected against the tool's schema and go back to Claude.
