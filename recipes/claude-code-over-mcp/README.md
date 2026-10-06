# Claude Code, Cursor or Codex over MCP

Give your coding agent a disposable Linux machine over MCP, so it writes code, installs packages and runs tests there instead of on your laptop. Then read back exactly what it did.

There is nothing to install for the agent side: one URL, one API key and one header. This folder adds two scripts: `verify.py` prints the sandbox's audit trail, and `simulate_agent.py` plays the coding agent so you can try the whole thing without one.

## What you need

- A NeevCloud account with a project [API key](https://docs.ai.neevcloud.com/getting-started/create-api-key) created with Resource Type **Sandboxes** (`NEEV_API_KEY`)
- Claude Code, Cursor or Codex
- For the scripts: Python 3.11 or later, and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`) for the project the key belongs to
- For `simulate_agent.py` only: a second key with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)

## 1. Put the key in your environment

The agent reads the key from the environment it was started from. The configs below only refer to `NEEV_API_KEY`, so the key itself is never written into a config file.

macOS and Linux, in `~/.zshrc` or `~/.bashrc`:

```bash
export NEEV_API_KEY=sk-nc-...
```

Windows PowerShell, saved for your user:

```powershell
[Environment]::SetEnvironmentVariable("NEEV_API_KEY", "sk-nc-...", "User")
```

Then open a new terminal and start your agent from it, so it inherits the variable.

## 2. Add the MCP server

Pick a sandbox name: lowercase letters, digits and hyphens, starting with a letter, at most 63 characters. The `x-sandbox-name` header binds the connection to that one sandbox; another name is another sandbox. The examples use `my-coding-box`.

**Claude Code.** Run this from your project folder:

```bash
claude mcp add --transport http --scope project neev-sandbox \
  https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp \
  --header 'Authorization: Bearer ${NEEV_API_KEY}' \
  --header 'x-sandbox-name: my-coding-box'
```

In PowerShell, put it on one line or end each line with a backtick instead of `\`. The single quotes matter in both shells: they keep `${NEEV_API_KEY}` as text, so Claude Code writes the reference, not your key, into `.mcp.json`:

```json
{
  "mcpServers": {
    "neev-sandbox": {
      "type": "http",
      "url": "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp",
      "headers": {
        "Authorization": "Bearer ${NEEV_API_KEY}",
        "x-sandbox-name": "my-coding-box"
      }
    }
  }
}
```

Claude Code fills in `${NEEV_API_KEY}` from the environment when it starts the server. It does this only for the project's `.mcp.json`, which is why the command uses `--scope project`. You can also write the file by hand. Start Claude Code, approve the `neev-sandbox` server when it asks, and check it under `/mcp`.

**Cursor.** Create `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project ([Connect Cursor](https://docs.ai.neevcloud.com/getting-started/mcp/connect-cursor) in the docs):

```json
{
  "mcpServers": {
    "neev-sandbox": {
      "url": "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:NEEV_API_KEY}",
        "x-sandbox-name": "my-coding-box"
      }
    }
  }
}
```

**Codex.** Add this to `~/.codex/config.toml` (`%USERPROFILE%\.codex\config.toml` on Windows). Codex reads the key from the variable named in `bearer_token_env_var` ([Connect Codex](https://docs.ai.neevcloud.com/getting-started/mcp/connect-codex) in the docs):

```toml
[mcp_servers.neev-sandbox]
url = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
bearer_token_env_var = "NEEV_API_KEY"
http_headers = { "x-sandbox-name" = "my-coding-box" }
```

Restart the agent after changing its MCP config.

## 3. Give it a task

Paste this prompt into the agent:

```text
Create a sandbox, then in its app folder write a small Node.js HTTP server (server.js) that answers GET /health with the JSON {"ok":true}, and one test (server.test.js) that uses node:test to start the server on a free port and check that reply. Use only Node's built-in modules, no npm packages. Run the tests with `node --test` from the app folder and tell me the result.
```

The MCP server does not create a sandbox by itself. Until one exists, every tool answers `no sandbox is bound to this connection: create one`, and the agent then calls `create_sandbox`, which creates the sandbox your `x-sandbox-name` names. The prompt asks for it up front so the agent does not have to find out. The sandbox comes with Node.js, npm and Python.

A new sandbox has no internet access, which is why this prompt avoids npm packages.

### Letting it install npm packages

No MCP tool can open the network: what a sandbox may reach is set by you through the API or the SDK, never by the agent. To let it install from npm, create the sandbox yourself before giving the task, with only the npm registry allowed:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python -c "from neevai import NeevAI; NeevAI().sandboxes.create({'name': 'my-coding-box'}, allow_egress=['registry.npmjs.org'])"
```

Then ask for something like "Write a small Express app in the app folder with one test, install it with npm, and run its tests". The agent finds the sandbox already exists and works in it; `npm install express` succeeds because the registry is on the sandbox's allow-list. See [Internet Access and Egress](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access) for the allow-list rules.

## 4. See what it did

While the sandbox still exists, run:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python verify.py my-coding-box
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

It looks the sandbox up by name, reads its audit trail page by page from the moment it was created, and prints one line per operation, oldest first. For a sandbox where a file was written and read, a command run, a server started and a folder listed:

```text
$ python verify.py coding-agent-cbdc5d75
UTC       operation       program     target                          outcome                    took  credential
15:34:18  fs.write        -           app/server.js                   success                     2ms  xxxxxxxx
15:34:18  fs.read         -           app/server.js                   success                     1ms  xxxxxxxx
15:34:18  exec            -           -                               success                   146ms  xxxxxxxx
15:34:18  process.start   node        -                               success                     5ms  xxxxxxxx
15:34:18  fs.list         -           app                             success                     1ms  xxxxxxxx

5 operations, 0 ended in an error. Operations per credential:
  xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx: 5
```

- **operation** is what happened: a file read, write or listing, a command, a process start.
- **program** is the program's name where the trail records one, such as for a process start. It is never its arguments, so secrets passed on a command line do not end up in the trail. Commands run through the MCP `exec` tool show `-` here.
- **target** is the file or folder acted on.
- **outcome** says whether the operation itself completed, not the program's exit code: a test run that fails still reads `success`. `error` comes with a reason, such as `not_found` or `invalid_argument`.
- **credential** identifies the API key the operation ran under (the full ID is in the summary). Work done over MCP is recorded under the key the agent connected with, just like SDK calls, so give each agent its own key if you want to tell them apart.

Records appear a few seconds after the work, so `verify.py` waits a moment and re-reads the trail until it stops changing. It exits 1 if the sandbox does not exist or nothing was recorded.

## 5. Clean up

Ask the agent to delete the sandbox (it calls `delete_sandbox`), or delete it from the console. Run `verify.py` first: the trail is read by sandbox name, so it cannot be looked up once the sandbox is gone. Until it is deleted, the sandbox counts against your project's sandbox quota.

## Try it without a coding agent

`simulate_agent.py` runs the same session with the MCP Python client and a model served by NeevCloud, then checks the result.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python simulate_agent.py
```

From a real run:

```text
1. Connecting a glm-4-7 coding agent to the sandbox MCP server as coding-agent-83a451aa (no sandbox exists yet)
   step 1: create_sandbox
   step 2: get_sandbox
   step 3: get_sandbox
   step 4: fs_write app/server.js
   step 5: fs_write app/server.test.js
   step 6: exec node --test
   step 7: finish
2. Agent says: Tests passed: 1/1. The Node.js HTTP server successfully responds to GET /health with {"ok":true}, and the node:test runner confirms the server behavior on a free port.
3. Ran node --test in the sandbox: 1 passed, 0 failed
4. What the agent did, from the sandbox's audit trail:
   UTC       operation       program     target                          outcome                    took  credential
   15:43:42  fs.write        -           app/server.js                   success                     2ms  xxxxxxxx
   15:43:47  fs.write        -           app/server.test.js              success                     1ms  xxxxxxxx
   15:43:49  exec            -           -                               success                   913ms  xxxxxxxx
   15:43:52  exec            -           -                               success                   959ms  xxxxxxxx

   4 operations, 0 ended in an error. Operations per credential:
     xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx: 4
   Sandbox deleted.
```

The script's own test run (line 3 of the output) is in the trail too, as an `exec` under the same key.

### How it works

1. The script connects to the MCP server with `x-sandbox-name: coding-agent-<random>`, a sandbox that does not exist yet, exactly as your coding agent would.
2. The model gets six tools from the server's own tool list: `create_sandbox`, `get_sandbox`, `fs_write`, `fs_read`, `fs_list` and `exec`. It is given the prompt from section 3 and creates the sandbox itself. Any other tool, or a `create_sandbox` for a different name, is refused.
3. When the model says it is done, the script runs `node --test` in the sandbox itself rather than trusting the model's summary.
4. It reads the audit trail with `client.sandboxes.get(name)` and `sandbox.audit(cursor=...)`, the same code as `verify.py`.
5. It deletes the sandbox in a `finally` block, so it goes even if the model fails or you press `Ctrl+C`.

It exits 0 only when the tests pass and the trail records the agent's file writes. The model is `glm-4-7` by default; set `MODEL` to try another, for example `MODEL=glm-5-2`. The agent gets at most 20 steps and 5 minutes.

## Time and cost

With your own coding agent, you pay for the sandbox while it exists and for your agent's own model as usual. `simulate_agent.py` took 28 to 38 seconds with `glm-4-7` in our runs, and 76 seconds with `glm-5-2`; you pay for under a minute of sandbox time and the model tokens of about ten short steps.

## Cleanup

`simulate_agent.py` deletes its sandbox when it ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `coding-agent-` sandbox from the console. A sandbox your own coding agent created stays until you or the agent delete it.
