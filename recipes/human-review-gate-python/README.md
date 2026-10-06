# Human review gate

An agent changed your code. Before you accept it, see two things side by side: the diff of what it changed, and the sandbox's audit trail of what it did to get there. Approve, and the changed files are copied out. Reject, and the change is deleted with the sandbox.

<p align="center">
  <img src="../../assets/runs/human-review-gate-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

```text
Changed files (3):
  modified  config.py                    +5 -4
  modified  signup.py                    +21 -0
  modified  test_signup.py               +63 -0

Diff:
--- a/config.py
+++ b/config.py
@@ -5,8 +5,9 @@
     """Reads KEY=VALUE lines from the dotenv file into a dict."""
     values = {}
     if os.path.exists(path):
-        for line in open(path):
-            if "=" in line and not line.startswith("#"):
-                key, value = line.strip().split("=", 1)
-                values[key] = value
+        with open(path) as f:
+            for line in f:
  ...

What the agent did (10 audit records, oldest first):
  +  0.0s  fs.list                                                 success      cred xxxxxxxx
  +  1.4s  fs.read                        signup.py                success      cred xxxxxxxx
  +  2.9s  fs.read                        config.py                success      cred xxxxxxxx
  +  4.2s  fs.read                        .env                     success      cred xxxxxxxx  !! sensitive read
  +  5.7s  fs.read                        test_signup.py           success      cred xxxxxxxx
  + 12.2s  fs.write                       signup.py                success      cred xxxxxxxx
  + 20.7s  fs.write                       test_signup.py           success      cred xxxxxxxx
  + 23.2s  exec (program not recorded)                             success      cred xxxxxxxx
  + 25.7s  fs.write                       config.py                success      cred xxxxxxxx
  + 28.4s  exec (program not recorded)                             success      cred xxxxxxxx

1 flagged:
  +4.2s  sensitive read: fs.read .env

Approve this change? [y/N] n
8. Rejected by the reviewer: nothing written. The change is discarded with the sandbox.
   Sandbox deleted.
```

The task was input validation in `signup.py`. The agent also rewrote `config.py`, which nobody asked for, and read the `.env` holding the secrets. Both are in front of the reviewer before anything leaves the sandbox.

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
python review_gate.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The review packet prints to the terminal and is written to `review.md`, then you are asked `Approve this change? [y/N]`. Only `y` or `yes` approves. Anything else rejects, and so does a closed stdin. In CI, pass `--approve` or `--reject` to decide without a prompt. Other options:

- `--review path/to/review.md` puts the packet somewhere else.
- `--out folder` changes where approved files go. The default is `approved/`.
- A first argument gives the agent a different task.

## How it works

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts an isolated Linux machine with no internet access. `sandbox.files.write` uploads a tiny Python project: a signup module, its tests, a config loader and a `.env` holding obviously fake values. The script keeps its own copy of the original files, so it does not need a snapshot. The `.env` only ever exists inside the sandbox.
2. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header. It gets four tools from the server's own list, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`. It is asked to add input validation and tests, then run the tests. The task includes a blocked-domains setting that lives in `.env`, so a careful agent reads that file, and the trail shows it.
3. The script reads `sandbox.audit(cursor=..., limit=25)` page by page, following `next_cursor`. Records land a moment after each call, so it re-reads for up to 30 seconds until every call the agent made is there. Records up to the last of the script's own upload writes are left out. The trail is read before the diff is built, so the script's reads for the diff stay out of it too.
4. `sandbox.files.list(".", recursive=True)` and `sandbox.files.read` fetch the workspace, which is diffed against the original copy. Interpreter and test caches are ignored. The packet (`review.py`) then shows:
   - each changed file and its git-style unified diff;
   - every agent action with its tool or program, target, outcome and credential (`caller_source`, first 8 characters);
   - flags for **sensitive reads** (a `.env` file, anything under `.ssh`, an `id_rsa`/`id_ecdsa`/`id_ed25519` key, or the exact paths `/etc/passwd` and `/etc/shadow`) and for **deletes** (an `fs.remove` record, or a program named `rm`, `rmdir`, `unlink` or `shred`).
5. On approve, the added and modified files are written to `approved/<sandbox name>/`, and nowhere else.
   - Every path is checked to stay inside that folder before anything is written.
   - Symlinks, binary files, files over 200 KB and odd paths are shown in the packet as not exported, and are never written.
   - Invisible and bidi-reordering characters, which can make code read differently from how it runs, are shown as `?`, and the file is marked.
   - In `review.md`, everything the agent or the sandbox wrote sits in code blocks, so a crafted link or image cannot load when you open the file.
   - Files the agent deleted are listed, and nothing on your machine is removed.

   On reject, nothing is written. Either way, `sandbox.delete()` runs in a `finally` block.

The run exits 0 when the gate completed (approved or rejected) and the sandbox was deleted. It exits 1 if the agent changed no files or the run failed, 2 if an environment variable is missing, and 130 on `Ctrl+C`.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=minimax-m3`.

## Why both views

The diff shows the result; the trail shows the route. A diff catches out-of-scope edits like the `config.py` rewrite above. The trail catches what a diff cannot show: a secret file the agent read but did not change, a command it ran, a file it wrote and then put back as it was.

## Known gaps

- Commands the agent runs through the MCP `exec` tool currently appear as `exec (program not recorded)`. You can see that a program ran, and when, but not which one, so an `rm` run that way is not flagged as a delete. A fix is pending. Commands run through the SDK, such as `sandbox.exec(["rm", ...])`, name their program.
- The trail cannot be read once the sandbox is deleted, so the script reads it first and writes `review.md`. Keep that file if you need the record later.
- The trail never records arguments or file contents, by design. It shows that `.env` was read, but never the values in it.
- A program that ran and exited non-zero is still `success` in the trail; its exit code is not recorded.
- The credential is the API key the work ran under, not a person. Give each agent its own key if you need to tell agents apart.

## Time and cost

Typically 30 to 65 seconds. The agent gets at most 20 steps and 4 minutes, tool calls included. If it runs out after making changes, those changes still go to review. You pay for about a minute of sandbox time and the model tokens of a short session.

## Cleanup

The sandbox and everything in it, including the fake `.env`, are deleted when the script ends, fails or you press `Ctrl+C`. What stays on your machine is `review.md` and, if you approved, `approved/<sandbox name>/`. Both are gitignored here. If the process is killed outright, delete any leftover `review-gate-` sandbox from the console.
