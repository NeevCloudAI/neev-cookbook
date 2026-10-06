# Audit evidence pack

Your auditor asks what your agents did last month, and under which credentials. This recipe exports your sandboxes' audit trails for a time window into a pack you can hand over: every recorded operation as CSV, a summary per API key, and a SHA-256 manifest that shows if any file was changed afterwards.

<p align="center">
  <img src="../../assets/runs/audit-evidence-pack-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). No model key: nothing here calls a model.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python evidence_pack.py --demo
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

`--demo` creates two short-lived sandboxes, does some typical work in them (one through the SDK, one over MCP), exports the pack and deletes them. To export your own sandboxes, name them:

```bash
python evidence_pack.py --sandbox billing-agent --sandbox support-agent --since 2026-09-01 --until 2026-10-01
```

- `--since` / `--until`: a date, a time such as `2026-09-01T09:00:00+05:30`, or `7d` / `12h` ago. The default is the last 30 days, which is all the trail the platform keeps.
- `--out`: the pack folder (default `evidence-pack-<UTC time>`). It must be new or empty.

The script exits 0 only when the pack was written and its manifest verified.

## What is in the pack

- `records.csv`: one row per recorded operation across all the sandboxes, oldest first: time, sandbox, API key, operation, program, target path, outcome, duration and a `sensitive` flag.
- `credentials.csv`: one row per API key: the sandboxes it touched, its operation, error and sensitive counts, and its first and last activity.
- `summary.md`: the page an auditor reads first: the window covered per sandbox, each key's activity, the errors and sensitive operations, and what the trail does and doesn't record.
- `MANIFEST.sha256`: the SHA-256 of the three files above.

Sensitive means reads of `.env` files, SSH keys, `/etc/passwd` or `/etc/shadow`; deletes; and terminal sessions.

## Verify a pack you received

Inside the pack's folder, run `shasum -a 256 -c MANIFEST.sha256` (macOS) or `sha256sum -c MANIFEST.sha256` (Linux). Then compare the SHA-256 of `MANIFEST.sha256` itself with the one the sender gave you through another channel, such as the email or ticket the pack came with. The manifest catches an edited file; the separate hash catches an edited manifest.

## How it works

1. **Read.** For each sandbox, the script reads its whole audit trail for the window with `client.sandboxes.audit(...)`. It fails rather than export a trail it had to cut short, or a sandbox it can't find.
2. **Merge.** It merges all the records oldest first, groups them by API key, counts errors and flags sensitive operations.
3. **Write.** It writes the two CSVs, the summary and the manifest. CSV cells that a spreadsheet would run as formulas are escaped, and file names in the summary can't inject links or images.
4. **Verify.** It reads every file back from disk and checks it against the manifest before reporting success.

## Use it in your product

- **Monthly evidence:** run the export on a schedule, for example the first of each month for the month before, and store the packs where your auditors can reach them.
- **Before deleting a sandbox:** a deleted sandbox's trail can't be read at all, so export it first if you may need the record.
- **Per customer or per team:** give each agent, team or tenant its own API key, and the pack separates their activity.
- **In your own tools:** `read_trail()` in `evidence_pack.py` and `write_pack()` in `pack.py` are plain functions you can call from your own code.

## What the trail records

- When the operation happened, what it was, the program (never its arguments), the path, the outcome, how long it took, the API key and a request id. `ls -la` is recorded as `ls`; file contents and input are never recorded.
- The outcome is whether the call succeeded, not the program's exit code.
- The API key identifies a key, not a person. Work over MCP is recorded like SDK work, under the key that connected.
- Commands run through the MCP `exec` tool are currently recorded without their program name, and a refused MCP call adds a record with no operation name, shown as `(unnamed)`. The demo shows both.
- Lifecycle events (creating, pausing, deleting a sandbox) are not part of this trail.

This pack is evidence of what the audit trail recorded. It does not by itself make anyone compliant with SOC 2, RBI or DPDP requirements; which evidence a control needs is for you and your auditor to decide.

## Time and cost

`--demo` takes about 10 seconds and uses two sandboxes for that long; both are deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `evidence-` sandbox from the console. Exporting your own sandboxes creates nothing and only reads the audit API.
