# Claude Code, Cursor or Codex over MCP

Give your coding agent a disposable Linux machine over MCP, so it writes code, installs packages and runs tests there instead of on your laptop. Then read back exactly what it did.

There is nothing to install for the agent side: one URL, one API key and one header. This folder adds two scripts: `verify.py` prints the sandbox's audit trail, and `simulate_agent.py` plays the coding agent so you can try the whole thing without one.

<p align="center">
  <img src="../../assets/runs/claude-code-over-mcp.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

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

While the sandbox still exists, print its audit trail:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python verify.py my-coding-box
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

You get one line per operation, oldest first:

```text
UTC       operation       program     target                          outcome                    took  credential
15:34:18  fs.write        -           app/server.js                   success                     2ms  xxxxxxxx
15:34:18  exec            -           -                               success                   146ms  xxxxxxxx
15:34:18  process.start   node        -                               success                     5ms  xxxxxxxx
```

- **operation**: a file read, write or listing, a command, or a process start.
- **program**: the program's name where it is recorded, never its arguments. Commands run through the MCP `exec` tool show `-` for now.
- **outcome**: whether the operation completed, not the program's exit code. A failing test run still reads `success`.
- **credential**: the API key the operation ran under.

Records appear a few seconds after the work, so `verify.py` waits until the trail stops changing.

## 5. Clean up

Run `verify.py` first, then ask the agent to delete the sandbox (it calls `delete_sandbox`) or delete it from the console. The trail can't be read once the sandbox is gone, and until then the sandbox counts against your project's quota.

## Use it with your team

- **One sandbox per task:** the `x-sandbox-name` header picks the machine. Use one name per project or branch, and agents working on different tasks never share files.
- **One key per agent:** every operation is recorded under the API key that made it. Give each agent, or each person's agent, its own key to tell them apart in the trail.
- **Keep a record:** run `verify.py` before deleting a sandbox and save its output alongside the pull request the agent's work went into.
- **Control the network:** create the sandbox yourself with an allow-list, as in section 3, so the agent can reach only what the task needs.

## Try it without a coding agent

`simulate_agent.py` plays the coding agent: it connects to the MCP server like your agent would, has a NeevCloud model do the task from section 3, runs the tests itself, prints the audit trail and deletes the sandbox. In the same shell as section 4, add a **Model API** key and run it:

```bash
export NEEV_MODEL_API_KEY=...
python simulate_agent.py
```

The agent gets six of the server's tools: `create_sandbox`, `get_sandbox`, `fs_write`, `fs_read`, `fs_list` and `exec`. Any other tool, or a `create_sandbox` for a different name, is refused. The script exits 0 only when the tests pass and the trail records the agent's file writes. The model is `glm-4-7` by default; set `MODEL` to try another.

## Time and cost

With your own coding agent, you pay for the sandbox while it exists and for your agent's model as usual. `simulate_agent.py` takes 28 to 38 seconds with `glm-4-7`, under a minute of sandbox time plus about ten short model steps. It deletes its sandbox when it ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `coding-agent-` sandbox from the console.
