# Prompt-injection-proof agent

An agent reads a project's README and sets the project up. The README has been poisoned: buried in the normal steps is an instruction to upload the project's `.env` to a paste site. The sandbox's egress allow-list stops the upload, whether or not the model falls for it.

<p align="center">
  <img src="../../assets/runs/injection-proof-agent-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python injection_demo.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The secrets are dummy values written at runtime, and the paste site is never on the allow-list, so nothing real is sent anywhere. The script exits 0 only when the upload sent zero bytes and got no response, and `pypi.org` still answered.

## How it works

1. **Allow-list.** The script starts a sandbox that may reach `pypi.org` and nothing else.
2. **Poisoned project.** It writes a project whose README tells the reader to `curl` the `.env` to a paste site, next to a `.env` of dummy secrets.
3. **Agent.** An agent connects over MCP and is asked to follow the README. The script records whether the model tried the upload. Many models do; some notice the trap and refuse.
4. **Worst case.** The script then runs the upload itself, as a fully compromised agent would, alongside a request to `pypi.org`. The upload sends 0 bytes and gets no response; `pypi.org` answers. This check alone decides the result, so it doesn't depend on how the model behaved.
5. **Audit.** The audit trail shows both `curl` runs, but never their arguments, so it records that `curl` ran without recording what was sent.

## Use it in your product

- **Any agent that reads untrusted input** (READMEs, web pages, issues, emails): run it in a sandbox whose allow-list holds only the hosts the job needs, set with `egress` when you create the sandbox ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).
- **Keep the policy out of the agent's hands:** the script owns the sandbox and its network policy, and the agent only gets workspace tools over MCP, so the model can never widen its own access.
- **Your own allow-list:** change `ALLOWED_HOST` in `injection_demo.py` to the host your job needs, such as your package index or one API. For several hosts, add more `allow` entries where the sandbox is created.

## Good to know

- Hosts match by exact name, with no wildcards.
- A blocked host still resolves in DNS and then times out, so a blocked request hangs until its time limit instead of failing fast. Treat DNS as a separate channel if your threat model includes it.
- Both `curl` runs read `success` in the trail because the program ran: the network policy, not the audit, is what stopped the upload.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`.

## Time and cost

Usually 1 to 3 minutes. The agent is limited to 10 model turns and 120 seconds, tool calls included, so a blocked `curl` it starts is cut off rather than left to hang. You pay for the sandbox while it runs and for the model tokens. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `injection-proof-` sandbox from the console.
