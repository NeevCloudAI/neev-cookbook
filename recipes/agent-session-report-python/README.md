# "What did my agent do?" session report

An agent just worked inside your sandbox. This recipe answers what it actually did, from the sandbox's own audit trail rather than from the agent's word.

<p align="center">
  <img src="../../assets/runs/agent-session-report-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

```text
Session report for session-report-3aee4d40: 13 records, 8 from the agent

Timeline (oldest first):
  +  0.0s  setup  fs.write                       README.md              success               2 ms  cred xxxxxxxx
  ...
  +  3.7s  agent  fs.list                        .                      success               1 ms  cred xxxxxxxx
  +  4.8s  agent  fs.read                        README.md              success               2 ms  cred xxxxxxxx
  +  7.9s  agent  exec (program not recorded)                           success              34 ms  cred xxxxxxxx
  + 12.5s  agent  fs.write                       SETUP_NOTES.md         success               1 ms  cred xxxxxxxx
  + 14.9s  agent  fs.read                        .env                   success                  -  cred xxxxxxxx  !! sensitive read

1 flagged:
  +14.9s  sensitive read: fs.read .env
```

## What you need

- Python 3.11 or later
- A NeevCloud account with two API keys from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key)):
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python session_report.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The report prints to the terminal and is written to `report.md` in the current directory (`--out path/to/report.md` to put it elsewhere; `report.md` is gitignored here). Pass your own task as the first argument to give the agent something else to do.

## How it works

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts an isolated Linux machine with no internet access, and `sandbox.files.write` seeds it with a tiny project: a README with setup steps, a config file, a stale lock file, and a `.env` holding obviously fake values. The `.env` only ever exists inside the sandbox.
2. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header and gets four tools from the server's own list, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`. It is asked to set the project up and summarise its configuration, which naturally means reading `.env`, running a program and deleting the lock file.
3. `sandbox.audit(cursor=..., limit=25)` is read page by page, following `next_cursor` until the trail is exhausted. Records land a moment after each call, so the script re-reads for up to 30 seconds until every call the agent made is there.
4. `report.py` orders the records oldest first and splits them into the script's setup and the agent's session (everything after the newest setup record, using the server's timestamps). It shows each record's tool or program, target, outcome, duration and credential (`caller_source`), counts calls and errors per tool and program, and flags:
   - **sensitive reads**: a read of a `.env` file, anything under `.ssh`, an `id_rsa`/`id_ecdsa`/`id_ed25519` key, `/etc/passwd` or `/etc/shadow`;
   - **deletes**: an `fs.remove` call, or a record whose program is `rm`, `rmdir`, `unlink` or `shred`. The agent here deletes the lock file with `exec rm` over MCP, which is not flagged today (see below); the same rule flags deletes in trails of SDK work.
5. `sandbox.delete()` runs in a `finally` block, so the sandbox is removed even if the agent fails or you press `Ctrl+C`.

The run exits 0 only when the report was written and contains at least one of the agent's actions.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

## What the trail does not record, and why that matters

A record names the operation, the program that ran and the path it acted on, the outcome, how long it took and the credential it ran under. It never records command arguments, file contents, or anything typed into a program, such as a password at a prompt. The agent above read `.env`, and the trail says so, but the secret values themselves never enter it. That is what makes the trail safe to keep for review and to share with an auditor.

Some things to know when reading a report:

- The outcome is whether the call succeeded. A program that ran and exited non-zero is still `success`; its exit code is not recorded.
- Commands the agent runs through the MCP `exec` tool currently appear as `exec (program not recorded)`, so an `rm` over MCP shows as an unflagged `exec` row. Commands run through the SDK, such as `sandbox.exec(["rm", ...])`, name their program and a delete among them is flagged.
- The credential is the API key the call was made under, not a person. Work done over MCP is recorded under the key that connected, just like SDK calls, so give each agent its own key if you need to tell agents apart.
- Sandbox lifecycle events (created, paused, deleted) are not part of this trail.
- The trail is kept for 30 days.

## Time and cost

Typically 20 to 40 seconds with `glm-4-7`; slower models can take a couple of minutes. The agent gets at most 15 steps and 3 minutes, tool calls included. You pay for under a minute of sandbox time and the model tokens of a short session.

## Cleanup

The sandbox and everything in it, including the fake `.env`, are deleted when the script ends, fails or you press `Ctrl+C`. The report on your machine is the only thing left. If the process is killed outright, delete any leftover `session-report-` sandbox from the console.
