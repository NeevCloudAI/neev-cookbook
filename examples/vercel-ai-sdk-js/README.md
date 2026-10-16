# Vercel AI SDK with NeevCloud Sandboxes

An AI SDK agent with the NeevCloud sandbox tools (run_command, write_file, read_file, stat_path, move_path and delete_path, plus opt-in expose_port, checkpoint/rollback and start_process/read_process_output/stop_process/list_processes), built by `toAiSdk` from `@neevcloud/sdk/agent-tools/ai-sdk`.

<p align="center">
  <img src="../../assets/runs/vercel-ai-sdk-js.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

## Run it

You need Node 20+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id).

```bash
npm install

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_API_KEY=...
# export ALLOW_PREVIEW=1
# export ALLOW_ROLLBACK=1
# export ALLOW_PROCESSES=1

npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The model endpoint defaults to `https://inference.ai.neevcloud.com/v1`; override
it with `NEEV_MODEL_BASE_URL` for any other OpenAI-compatible provider.

The agent writes `primes.py`, runs it, and reports the output.

Output:

```
  sandbox ready: ai-sdk-<id>

  agent: The program printed:

[2, 3, 5, 7, 11, 13, 17, 19, 23, 29]

  sandbox deleted
```

## Use the MCP server instead

`mcp-agent.js` runs the same task with no tool glue: `experimental_createMCPClient` from `@ai-sdk/mcp` connects to the NeevCloud sandbox MCP server over streamable HTTP, the `x-sandbox-name` header picks the sandbox, and the model gets only `exec`, `fs_read`, `fs_write`, `fs_exists`, `fs_move` and `fs_remove` (`ALLOW_PROCESSES=1` adds `process_start`, `process_get`, `process_logs`, `process_kill` and `process_list`). The script creates and deletes the sandbox itself over the same connection and closes the client at the end. Prefer it when you want no glue code, the same tools Claude Code and Cursor use, and calls that wake a paused sandbox; the trade-off is that the server decides output size and error text. It needs no organization or project IDs, and `NEEV_REGION` defaults to `as-south-1`.

```bash
npm run mcp
```

## Files

- `agent.js` — passes `toAiSdk(tools)`'s tools and its guard's `signal` to `generateText`, calls `guard.raiseIfFailed()` afterwards, and runs one task.
- `sandbox-agent.js` — creates a sandbox, runs the agent in it, and deletes it.
- `mcp-agent.js` — the same task over the sandbox MCP server, with no tool glue.

## Contract notes

- A path outside `/workspace`, such as `/etc/passwd` or `../etc/passwd`, comes back as an error result: `HTTP 400 invalid_argument: path "/etc/passwd" escapes workspace root`.
- Absolute paths inside `/workspace` work, and commands run in `/workspace` by default.
- A command still running after `timeout_s` comes back as an error result: `command did not finish within N s`.
- Command output that is not UTF-8 keeps its text, with U+FFFD in place of the bad bytes; a file that is not UTF-8 comes back from `read_file` as a `not UTF-8 text` error result.

## Traps

- A command that prints nothing for about 2 minutes is cut off by the network in front of the sandbox; `run_command` then returns an error result telling the model to use `start_process`. Set `ALLOW_PROCESSES=1` for builds, test suites, servers and other long or quiet work.
- Without `stopWhen: stepCountIs(n)` the SDK stops after the first tool call and never comes back with a final answer.
- The SDK turns every error a tool's `execute` throws into an error result for the model, so a bad `NEEV_API_KEY` or a network failure loops until the step limit and the run still ends in success. `toAiSdk`'s guard answers the model with a fixed message instead and aborts its `signal`; pass it as `abortSignal` and call `guard.raiseIfFailed()` in a `finally`, so the sandbox error is thrown rather than the abort. Arguments that do not fit a tool's schema go back to the model as an error result.
