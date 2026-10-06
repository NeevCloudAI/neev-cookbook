# Live egress approval

An agent starts with no internet. When its task needs a host, it asks for it, you answer `y` or `N` in
the terminal, and an approved host is added to the running sandbox's allow-list on the spot, with no
restart. A denied request goes back to the agent as a refusal, and the host is never added.

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
python egress_approval.py
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

The default task needs two hosts: `api.github.com` for a repository's latest release and `pypi.org` to
compare the version. Approve one and deny the other to see both paths. Pass your own task as an argument,
for example `python egress_approval.py "pip install requests and print its version"`, and approve both
`pypi.org` and `files.pythonhosted.org` when asked.

To run unattended, for example in CI, decide up front:

```bash
python egress_approval.py --auto-approve api.github.com --auto-deny
```

`--auto-approve HOST` (repeatable) approves that host without asking. `--auto-deny` refuses every other
host without asking. Without `--auto-deny`, any other host is put to you; if there is no terminal to
answer from, the answer is no.

The script exits `0` only when the agent finished its task, the first approved host was blocked before
its approval, every approved host was reachable after its approval, and every host nobody approved (each
denied host, plus `example.com`, which is never requested) is still blocked at the end.

## What you see

```
1. Creating a sandbox with an empty egress allow-list (no internet)...
2. Asking glm-4-7: Find the latest release tag of the GitHub repository astral-sh/uv using the GitHub API (api.github.com), and check whether that version is also the latest uv on PyPI (pypi.org). Answer in two sentences.
   step 1: request_egress api.github.com
      reason: Fetch latest release tag for astral-sh/uv repository via GitHub API
      Allow this sandbox to reach api.github.com? [y/N] y
      approved (by you); before: blocked; added to the live allow-list, no restart; reachable 0.2s later
   step 1: request_egress pypi.org
      reason: Check latest version of uv package on PyPI
      Allow this sandbox to reach pypi.org? [y/N] n
      denied (by you); the allow-list is unchanged
   step 2: exec curl --max-time 15 https://api.github.com/repos/astral-sh/uv/releases/latest
   step 3: finish
   Agent's answer: The latest GitHub release tag for astral-sh/uv is 0.12.23. I could not verify if this matches the latest PyPI version because access to pypi.org was denied.
3. Allow-list now: api.github.com
4. Checking that hosts nobody approved are still blocked: pypi.org, example.com
   pypi.org: curl exit 28, http 000 (blocked)
   example.com: curl exit 28, http 000 (blocked)
5. Audit trail of the commands run (UTC time, program, outcome, key; arguments are never recorded):
   05:50:18 curl -> success  key xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   05:50:19 curl -> success  key xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   05:50:22 (program not recorded) -> success  key xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   05:50:30 curl -> success  key xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   05:50:35 curl -> success  key xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ok   the agent finished its task
   ok   at least one host was approved
   ok   api.github.com was blocked before its approval
   ok   api.github.com was reachable after its approval
   ok   pypi.org stayed blocked
   ok   example.com stayed blocked
Done: approved hosts opened live without a restart, everything else stayed blocked.
   Sandbox deleted.
```

In the audit trail, the first two `curl` lines are the script checking `api.github.com` before and after
the approval, the middle line is the agent's own `curl` (commands the agent runs over MCP currently appear
without their program name; that is being fixed on the platform), and the last two are the final checks
of `pypi.org` and `example.com`. They all read `success` because the program ran; the egress policy, not
the audit, is what stopped the blocked connections. `key` is the ID of the API key the command ran under, masked by the script so the output is safe to share.

## How it works

The script holds the sandbox and its egress policy; the agent only gets workspace tools over MCP and one
local tool to ask for a host, so the model can never widen its own network access.

1. `client.sandboxes.create({"egress": {"mode": "allow_list", "allow": []}})` starts a sandbox whose
   allow-list is empty, so nothing outbound connects. It is created in `allow_list` mode rather than
   `deny_all` because hosts can only be added to an existing allow-list; a `deny_all` sandbox rejects
   `egress_add` and needs its whole policy replaced first.
2. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header and keeps
   four of its tools: `fs_write`, `fs_read`, `fs_list` and `exec`. It also gets two local tools:
   `request_egress(host, reason)` and `finish`.
3. Each `request_egress` call is checked before anyone sees it: it must be one exact hostname, so a
   URL, a wildcard, an IP address or a CIDR such as `0.0.0.0/0` is refused. Everything the model writes is
   stripped of terminal control characters before it is printed, so it cannot hide or rewrite the prompt.
   Each host is decided once, and at most four are asked about.
4. On approval, the script shows the host is blocked, calls
   `sandbox.update({"egress_add": {"allow": [{"host": host}]}})`, and polls with `curl` until the host
   answers. In our runs that took 0.2 to 0.4 seconds. The sandbox is updated in place, not restarted.
   On denial the allow-list does not change and the agent is told not to try.
5. After the agent finishes, the script prints the allow-list from `sandbox.refresh()`, checks that
   every host nobody approved still times out, and reads `sandbox.audit(from_=...)` for the commands run.

`sandbox.delete()` runs in a `finally` block, so the sandbox is removed even if the agent fails or you
press `Ctrl+C`, including at the approval prompt.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example
`MODEL=minimax-m3` or `MODEL=glm-5-2`.

## Time and cost

About 40 seconds unattended, plus however long you take to answer. The agent is bounded to 16 model
turns and 180 seconds, with its tool calls counted against the same budget. Time spent in an approval,
both your answer and the script's reachability checks (at most about 35 seconds per host), is not counted. You pay for the sandbox while it runs and for the model tokens the agent uses.

## Cleanup

The sandbox and everything in it are deleted when the script ends, fails or you press `Ctrl+C`. If the
process is killed outright, delete any leftover `egress-approval-` sandbox from the console.
