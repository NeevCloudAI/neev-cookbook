# Prompt to live app

Describe a web app in one sentence. An AI agent builds it inside an isolated NeevCloud sandbox and hands you a public URL you can open on your phone.

<p align="center">
  <img src="../../assets/prompt-to-live-app.png" alt="A todo app built by the agent, open on its preview URL" width="560">
</p>

## What you need

- Node 20 or later
- A NeevCloud account with two API keys from **Account > API Keys**:
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)
- Your organization and project IDs (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
npm install
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start -- "a pomodoro timer with a calm green theme"
```

The app stays online for 10 minutes (`--keep 0` to stop as soon as it is up). Press `Ctrl+C` to stop sooner.

## How it works

The script and the agent hold different powers. The script uses the SDK for the lifecycle; the agent only gets workspace tools over MCP.

1. `neev.sandboxes.create({ egress: { mode: "deny_all" } })` starts an isolated Linux machine with no internet access.
2. The agent (`agent.ts`) connects to the sandbox MCP server with the `x-sandbox-name` header, so its session is bound to that one sandbox. It reads the tool list from the server and keeps four tools, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`. Lifecycle tools such as `delete_sandbox` are never offered, and a call to one is refused.
3. `sandbox.processes.start("python3", { args: ["-m", "http.server", "3000", "--bind", "0.0.0.0"] })` serves the files. The server binds `0.0.0.0` so the preview URL can reach it.
4. `sandbox.getUrl({ port: 3000 })` exposes the port and waits until the URL answers.
5. `sandbox.delete()` runs in a `finally` block, so the sandbox is removed even if the agent fails or you press `Ctrl+C`.

Every tool call the agent makes is recorded in the sandbox audit trail under your API key.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

## Time and cost

Typically 1 to 2 minutes from start to URL, plus the time you keep it online. The agent gets at most 25 steps and 4 minutes; if it runs out after writing `index.html`, you get what it wrote so far. You pay for the sandbox while it runs and for the model tokens the agent uses.

## Cleanup

The sandbox and everything in it are deleted when the script ends, fails or you press `Ctrl+C`. The preview URL stops working at the same moment. If the process is killed outright, delete any leftover `live-app-` sandbox from the console.
