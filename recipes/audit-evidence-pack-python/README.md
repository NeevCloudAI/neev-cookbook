# Audit evidence pack

Your auditor asks what your agents did in production last month, and under which credentials. This recipe exports the sandboxes' own audit trails for a time window into a pack you can hand over: every recorded operation as CSV, a per-credential summary, and a SHA-256 manifest that shows if any file was changed afterwards.

<p align="center">
  <img src="../../assets/runs/audit-evidence-pack-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

```text
1. Creating two sandboxes with no internet access: evidence-sdk-dd3b4abd, evidence-mcp-dd3b4abd
2. Working in evidence-sdk-dd3b4abd through the SDK:
   wrote report.txt and a .env with a dummy value, read the .env back
   ran ls -la, and ls on a missing folder (exit code 2)
   read missing.txt: refused, not found (as intended)
   started sleep 30 in the background, removed report.txt
3. Working in evidence-mcp-dd3b4abd over MCP:
   fs_write notes.txt
   exec sh -c 'wc -c notes.txt'
   fs_read missing.txt: the sandbox refused this call: not_found: file not found: "missing.txt" (as intended)
   process_start sleep 30
4. Waiting for both audit trails to show every operation...
5. Exporting 2026-10-05T16:48:28Z to 2026-10-05T16:58:36Z, before the sandboxes are deleted:
   13 records from 2 sandboxes:
     evidence-sdk-dd3b4abd            8 records
     evidence-mcp-dd3b4abd            5 records
   Per credential:
     xxxxxxxx     13 records  2 errors  2 sensitive  in evidence-sdk-dd3b4abd, evidence-mcp-dd3b4abd
6. Pack written to evidence-pack-20261005T165328Z/ and verified against MANIFEST.sha256.
   SHA-256 of MANIFEST.sha256: 1139fc24a179b64092c00e80e41a347a01c390609abfcc2c97fcfe3dbdd574bd
   Hand this hash over separately from the pack, so the recipient can tell if anything was changed.
   Sandbox evidence-sdk-dd3b4abd deleted.
   Sandbox evidence-mcp-dd3b4abd deleted.
```

## What you need

- Python 3.11 or later
- A NeevCloud API key with Resource Type **Sandboxes** (`NEEV_API_KEY`), from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key)). No model key is needed: nothing here calls a model.
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python evidence_pack.py --demo
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

`--demo` creates two short-lived sandboxes, does some representative work in them, exports the pack, and deletes the sandboxes. To export your own sandboxes instead, name them:

```bash
python evidence_pack.py --sandbox billing-agent --sandbox support-agent --since 2026-09-01 --until 2026-10-01
```

`--since` and `--until` take a date, an RFC 3339 time (`2026-09-01T09:00:00+05:30`), or `7d` / `12h` before now; a time without a zone is UTC. The default window is the last 30 days, which is all the trail the platform keeps. The pack goes to a new folder, `evidence-pack-<UTC time>` by default or `--out FOLDER`, and the script refuses a folder that already holds files, so a pack only ever contains its own.

The script exits 0 only when the pack was written and its manifest verified, and, with `--demo`, every demo sandbox was deleted.

## What is in the pack

- `records.csv`: one row per audit record, oldest first, across all the sandboxes: time, sandbox name and id, record id, credential, operation (`tool`), program (`command`), target path, outcome, reason code, duration, request id, and a `sensitive` flag.
- `credentials.csv`: one row per credential: the sandboxes it touched, its record, error and sensitive counts, and its first and last activity in the window.
- `summary.md`: the page an auditor reads first. The window asked for and the window each sandbox's trail actually covered, a table per credential, the operations each credential made with their error counts, the errors and sensitive operations (the first 50 of each; `records.csv` always has them all), what the trail does and does not record, and how to verify the pack.
- `MANIFEST.sha256`: the SHA-256 of the three files above, in the format `shasum -a 256 -c` and `sha256sum -c` read.

Flagged as sensitive: reads of `.env` files, SSH keys, `/etc/passwd` or `/etc/shadow`; deletes (`fs.remove`, or the programs `rm`, `rmdir`, `unlink`, `shred`); and terminal sessions (`pty_command`, `ssh`).

To check a pack you received, run `shasum -a 256 -c MANIFEST.sha256` (macOS) or `sha256sum -c MANIFEST.sha256` (Linux) inside its folder, and compare the SHA-256 of `MANIFEST.sha256` with the one the sender gave you through another channel, such as the email or ticket the pack came with. The manifest on its own catches an edited file; the separately delivered hash catches an edited manifest.

## How it works

1. `client.sandboxes.audit(name, from_=..., to=..., cursor=..., limit=200)` reads one page of a sandbox's trail, newest first. The script fixes the window once and passes the same `from_` and `to` with every `next_cursor`, so records that arrive during the export cannot shift the pages. It stops when no cursor comes back, and fails rather than export a trail it had to cut short.
2. Each page reports the window the server actually served. A start older than the `retention_days` the platform keeps (30 on NeevCloud) is moved forward, `window_truncated` is set, and the summary says so for that sandbox.
3. A sandbox the API cannot find fails the whole export, naming it, and no pack is written. That includes a deleted sandbox: its trail can no longer be read, by name or by id.
4. `pack.py` merges every sandbox's records oldest first, groups them per credential (`caller_source`), counts errors (`outcome` other than `success`) and flags sensitive operations, then writes the CSVs, the summary and the manifest. CSV cells that begin with `=`, `+`, `-`, `@`, a tab or a carriage return get a leading `'` so a spreadsheet does not run them as formulas, and so do cells that already begin with `'`, so removing one leading `'` always gives the recorded value. In the summary, file names and programs are shown as code and escaped, so a crafted file name cannot add lines, links or images.
5. The script re-reads every file from disk and checks it against the manifest before it reports success.

In `--demo`, the script first creates two sandboxes with `client.sandboxes.create({"egress": {"mode": "deny_all"}})`. In one it works through the SDK: `sandbox.files.write`, `files.read_text` (including a read of a missing file), `sandbox.exec`, `processes.start` and `files.remove`. In the other it connects to the Sandbox MCP Server with the `x-sandbox-name` header and calls `fs_write`, `exec`, `fs_read` on a missing file and `process_start`. It waits up to 30 seconds until each trail shows every operation it made, exports, and only then deletes both sandboxes.

## What the trail records, and what it does not

These are the platform's behaviours as we observed them, and the summary in every pack repeats them:

- A record holds when the operation happened (server time), the operation, the program it ran without its arguments, the path it acted on, whether it succeeded and why not, how long it took, the credential and a request id. A record has no field for command arguments, file contents, or input sent to a program: `ls -la` is recorded as `ls`.
- The outcome is whether the call succeeded, not the program's exit code. In the demo, `ls` on a missing folder exits 2 and is recorded as `success`.
- The credential is an API key id. It identifies a key, not a person. Work done over the Sandbox MCP Server is recorded like SDK work, under the key that connected, with no mark that it came over MCP. The demo uses one key for both sandboxes, so the pack shows one credential; give each agent, team or tenant its own key and the pack separates them.
- Commands run through the MCP `exec` tool are currently recorded without their program name, and a call the sandbox refuses over MCP currently also adds a record with no operation name, shown as `(unnamed)`. You can see both in the demo.
- Lifecycle events (creating, pausing or deleting a sandbox) are not part of this trail.
- The trail is kept for 30 days, and a deleted sandbox's trail cannot be read at all. If you need evidence older than that, or for sandboxes you delete, export on a schedule and before deleting, and keep the packs yourself.

This pack is evidence of what the audit trail recorded. It does not by itself make anyone compliant with SOC 2, RBI or DPDP requirements; which evidence a control needs is for you and your auditor to decide.

## Time and cost

`--demo` takes about 10 seconds and uses two sandboxes for that long. An export of existing sandboxes creates nothing and only reads the audit API: one request per 200 records per sandbox.

## Cleanup

With `--demo`, both sandboxes are deleted when the script ends, fails or you press `Ctrl+C`, after the export if it got that far. If the process is killed outright, delete any leftover `evidence-` sandbox from the console. An export of your own sandboxes never creates or deletes anything. The pack folder on your machine is yours to keep; `evidence-pack*/` is gitignored here.
