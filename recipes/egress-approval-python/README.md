# Live egress approval

An agent starts with no internet. When its task needs a host, it asks for it; a person answers `y` or `N` in the terminal; and an approved host is added to the running sandbox's allow-list on the spot, with no restart. A denied host is never added.

<p align="center">
  <img src="../../assets/runs/egress-approval-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python egress_approval.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The default task needs two hosts, `api.github.com` and `pypi.org`. Approve one and deny the other to see both paths. Pass your own task as an argument, for example `python egress_approval.py "pip install requests and print its version"`, and approve `pypi.org` and `files.pythonhosted.org` when asked.

To run unattended, such as in CI, decide up front: `--auto-approve HOST` (repeatable) approves that host, and `--auto-deny` refuses every other host. With no terminal to answer from, the answer is no.

## How it works

1. **Empty allow-list.** The script starts a sandbox with an allow-list that holds no hosts, so nothing outbound connects.
2. **Agent.** An agent connects over MCP with workspace tools plus one local tool, `request_egress(host, reason)`. It can ask for a host, but only the script can grant it.
3. **Ask.** Each request is checked first: it must be one exact hostname, so a URL, a wildcard, an IP address or `0.0.0.0/0` is refused. Then you're asked, with the agent's reason.
4. **Grant.** On approval, `sandbox.update({"egress_add": ...})` adds the host to the live allow-list, and the script confirms it is reachable, usually within half a second. On denial nothing changes.
5. **Check.** At the end, the script confirms every host nobody approved is still blocked, and prints the commands from the audit trail.

## Use it in your product

- **Approvals in your UI:** the decision is `EgressGate` in `egress_approval.py`. Replace its terminal prompt with a button in your app, a chat message to an admin, or a policy lookup.
- **Standing policy:** approve your usual hosts with `--auto-approve` and send only the unusual ones to a person.
- **Least privilege by default:** start every agent with an empty allow-list and add hosts as the task proves it needs them ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).

## Good to know

- The sandbox starts in `allow_list` mode with no hosts, not `deny_all`, because hosts can be added to an existing allow-list on the fly; a `deny_all` sandbox needs its whole policy replaced.
- Each host is decided once, and at most four are asked about per run.
- Everything the model writes is stripped of terminal control characters before it is shown, so it can't hide or rewrite the prompt.
- In the audit trail, commands the agent runs over MCP currently show without their program name.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`.

## Time and cost

About 40 seconds unattended, plus the time you take to answer. The agent is limited to 16 model turns and 180 seconds, not counting time spent waiting for your approval. You pay for the sandbox while it runs and for the model tokens. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, including at the prompt. If the process is killed outright, delete any leftover `egress-approval-` sandbox from the console.
