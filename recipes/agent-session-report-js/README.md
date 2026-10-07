# "What did my agent do?" session report

An agent just worked inside your sandbox. This recipe tells you what it actually did, from the sandbox's own audit trail rather than the agent's account: every file it read or wrote and every command it ran, with sensitive reads flagged.

This is the TypeScript version of the [Python recipe](../agent-session-report-python).

<p align="center">
  <img src="../../assets/runs/agent-session-report-js.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Node 20.3+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
npm install
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The report prints to the terminal and is saved to `report.md` (change it with `npm start -- --out other.md`). Pass your own task as the first argument to give the agent something else to do: `npm start -- "List every setting the app reads"`.

## How it works

1. **Sandbox.** The script starts a sandbox with no internet access and seeds a tiny project: a README, a config file, a stale lock file and a `.env` with fake secrets.
2. **Agent.** An agent connects over MCP and is asked to set the project up, which naturally means reading `.env`, running a program and deleting the lock file.
3. **Trail.** The script reads the sandbox's audit trail with `sandbox.audit()`, waiting a few seconds for the last records to arrive.
4. **Report.** It splits the script's setup from the agent's session, lists the agent's actions oldest first, counts them by tool and program, and flags sensitive reads (`.env`, SSH keys, `/etc/passwd`) and deletes.

## Use it in your product

- **Report on any sandbox:** `readTrail()` in `session-report.ts` and `makeReport()` in `report.ts` work on any sandbox your agents use, not just this one. Run them before you delete the sandbox.
- **Show it to your users:** `toMarkdown()` gives the report as Markdown, ready for a dashboard, a ticket or a chat message.
- **Your own red flags:** extend `SENSITIVE` and `DELETE_PROGRAMS` in `report.ts` with the paths and programs that matter to you.

## What the trail records

Each record names the operation, the program and the path it acted on, the outcome, how long it took and the API key it ran under. It never records command arguments, file contents, or anything typed into a program. The agent here read `.env`, and the trail says so, but the secret values never enter it, which makes the trail safe to keep and share with an auditor.

- The outcome is whether the call succeeded, not the program's exit code.
- Commands the agent runs through the MCP `exec` tool currently show as `exec (program not recorded)`, so an `rm` run that way is not flagged as a delete. Commands run through the SDK name their program.
- Work over MCP is recorded under the key that connected. Give each agent its own key to tell agents apart.
- Lifecycle events (created, paused, deleted) are not in this trail. Records are kept for 30 days, but can't be read once the sandbox is deleted.

## Time and cost

Usually 30 to 40 seconds with `glm-4-7`; slower models take longer, about a minute with `minimax-m3`. The agent is limited to 15 steps and 3 minutes. You pay for under a minute of sandbox time plus the model tokens. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, and only the report stays on your machine. If the process is killed outright, delete any leftover `session-report-js-` sandbox from the console. The default model is `glm-4-7`; set `MODEL` to try another.
