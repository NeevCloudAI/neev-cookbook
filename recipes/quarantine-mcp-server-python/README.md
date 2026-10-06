# Quarantine an untrusted MCP server

Before you plug a third-party MCP server into your agent, run it where it can't hurt you. This recipe starts an untrusted server inside a NeevCloud sandbox with no internet access, calls its one tool, and then shows what it did behind the answer: the secrets it read, the data it tried to send out, and the background process it left running.

<p align="center">
  <img src="../../assets/runs/quarantine-mcp-server-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

The server here (`untrusted_server.py`) is a deliberate stand-in for a malicious one. Its `summarize_text` tool returns a harmless summary, but along the way it reads `~/.ssh/id_rsa` and a `.env`, tries to post them to an outside host, and starts a looping background beacon. The secrets are dummy values and the sandbox has no internet, so running it is safe.

## Run it

You need Python 3.11+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). No model key: no language model is involved.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python quarantine.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

Neither the MCP package nor the untrusted server ever runs on your machine. The script exits 0 only when the outbound upload was blocked and both suspicious behaviours, the secret reads and the background process, were seen.

## How it works

1. **Install.** The script starts a sandbox that can reach only the Python package index, and installs the MCP package inside it.
2. **Lock down.** It removes all internet access with `sandbox.update(...)` before the untrusted server ever runs.
3. **Call the tool.** It plants dummy secrets, uploads the server and a small MCP client, and runs the client in the sandbox. The client calls `summarize_text` once, exactly as your agent would.
4. **Inspect.** From outside, the script probes two hosts (the fake collection endpoint and the package index that was reachable a moment ago) to prove nothing can leave, lists processes to find the beacon, and reads the audit trail.

## Use it in your product

- **Vet a real server:** add its package to `PIP_INSTALL` in `quarantine.py`, and change the server command in `harness.py` (`SERVER`) to launch it. Then call its tools from the harness the way your agent would.
- **Run untrusted servers for good:** keep each third-party MCP server in its own locked-down sandbox and connect your agent to it there, rather than running it next to your secrets.
- **Allow only what it needs:** if a server legitimately calls one API, create the sandbox with an allow-list for that host instead of no internet ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).

## Good to know

- The audit trail records operations made through the sandbox (commands, file writes), not every system call a process makes. The server's own file reads and connection attempts don't appear there.
- That is why the network policy, not a log, is what protects you: the trail tells you what ran, and denied egress makes sure nothing it took can leave.
- The secret reads in the output come from the server's own record, which this stand-in keeps so you can see them.

## Time and cost

Usually under a minute, most of it installing the MCP package. You pay for that minute of sandbox time and nothing else; there is no model call. The sandbox, including the beacon, is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `quarantine-` sandbox from the console.
