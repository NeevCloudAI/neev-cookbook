# Claude Agent SDK (JavaScript) with NeevCloud Sandboxes

A Claude Agent SDK agent whose only tools are the NeevCloud sandbox tools (run_command, write_file, read_file, stat_path, move_path and delete_path, plus opt-in expose_port, checkpoint/rollback and start_process/read_process_output/stop_process/list_processes).

`toClaude` from `@neevcloud/sdk/agent-tools/claude` serves the tools in-process as
an SDK MCP server and switches Claude Code's built-in tools (Bash, Read, Write and
the rest) off, because they would act on the machine running this script rather
than the sandbox.

## Run it

You need Node 20+, an [Anthropic API key](https://console.anthropic.com/), a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id).

```bash
npm install

export ANTHROPIC_API_KEY=...
export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
# export ALLOW_PREVIEW=1
# export ALLOW_ROLLBACK=1
# export ALLOW_PROCESSES=1

npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The agent writes `primes.py`, runs it, and reports the output.

## Files

- `agent.js` — spreads `toClaude(tools).options()` into `query()`'s options, reads the run through `watch`, and runs one task.
- `sandbox-agent.js` — creates a sandbox, runs the agent in it, and deletes it.

## Traps

- A command that prints nothing for about 2 minutes is cut off by the network in front of the sandbox; `run_command` then returns an error result telling the model to use `start_process`. Set `ALLOW_PROCESSES=1` for builds, test suites, servers and other long or quiet work.
- Tool names reach Claude as `mcp__neev__<tool>`; `options()` already lists them in `allowedTools`.
- Claude Code's built-in tools act on your machine, not the sandbox. `options()` sets `tools: []`, which switches all of them off; a `disallowedTools` list would have to keep up with every new built-in.
- `options()` sets `strictMcpConfig: true`, which keeps MCP servers from your own Claude Code config out of the run.
- By default the Claude Agent SDK loads your `~/.claude` and project settings, including hooks that would run on your machine with sandbox output. `options()` sets `settingSources: []` to keep them out; a Claude Code login still authenticates.
- `allowedTools` only auto-approves. `options()` sets `permissionMode: 'dontAsk'`, which denies any other tool call instead of falling back to a permission prompt.
- The Claude Agent SDK turns every error a tool throws into an error result for Claude, so a bad `NEEV_API_KEY` or a network failure would loop until `maxTurns` and the run would still end in success. `toClaude` answers Claude with a fixed message instead (SDK errors can carry request URLs with org and project IDs), and `watch` stops at the next message, closes the query and rethrows the error. A run that ends without success, such as `error_max_turns`, throws too. Bad arguments are rejected against the tool's schema and go back to Claude.
- The Claude Agent SDK is ESM-only, and `toClaude` needs zod 4.2 or later.
- The tool schemas enforce `minimum`/`maximum` and number types before a tool runs, so an out-of-range `timeout_s` or `wait_s` (say 9999) or a number sent as a string goes back to Claude as an input error instead of being clamped or parsed as in the other examples.
