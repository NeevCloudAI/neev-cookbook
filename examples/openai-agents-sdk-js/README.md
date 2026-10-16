# OpenAI Agents SDK (JavaScript) with NeevCloud Sandboxes

An OpenAI Agents SDK agent with the NeevCloud sandbox tools (run_command, write_file, read_file, stat_path, move_path and delete_path, plus opt-in expose_port, checkpoint/rollback and start_process/read_process_output/stop_process/list_processes), built by `toOpenAIAgents` from `@neevcloud/sdk/agent-tools/openai-agents`.

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

## Files

- `sandbox-agent.js` — creates a sandbox, runs the agent in it with `toOpenAIAgents(new SandboxTools(sandbox))` as its tools, and deletes it.

## Contract notes

- A path outside `/workspace`, such as `/etc/passwd` or `../etc/passwd`, comes back as an error result: `HTTP 400 invalid_argument: path "/etc/passwd" escapes workspace root`.
- Absolute paths inside `/workspace` work, and commands run in `/workspace` by default.
- A command still running after `timeout_s` comes back as an error result: `command did not finish within N s`.
- Command output that is not UTF-8 keeps its text, with U+FFFD in place of the bad bytes; a file that is not UTF-8 comes back from `read_file` as a `not UTF-8 text` error result.

## Traps

- A command that prints nothing for about 2 minutes is cut off by the network in front of the sandbox; `run_command` then returns an error result telling the model to use `start_process`. Set `ALLOW_PROCESSES=1` for builds, test suites, servers and other long or quiet work.
- `setOpenAIAPI('chat_completions')` is required for non-OpenAI endpoints.
- `setTracingDisabled(true)` avoids trace uploads to OpenAI.
- The SDK turns every tool exception into a "please try again" message by default, hiding auth and network failures. `toOpenAIAgents` rethrows them instead, so they stop the run. The runner itself answers arguments that are empty or not JSON before the tool runs, and `call()` returns arguments that do not fit the schema as an error result.
