# Hosted coding agent

Run a coding agent on NeevCloud instead of your laptop, give it a task from a script, and check its work yourself. This recipe starts OpenCode from a ready-made agent template, has it fix a failing test suite with a NeevCloud model, verifies the fix, prints the audit trail, then pauses and resumes the agent to show it keeps its work.

<p align="center">
  <img src="../../assets/runs/hosted-agent-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). OpenCode uses the Model API key to call the model.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python hosted_agent.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The script exits 0 only when the test file is unchanged, the tests pass after the agent's fix, and the agent is the same running machine after the resume.

## How it works

1. **Agent.** `client.agents.create(...)` starts an agent from the `opencode` template, with internet access narrowed to the NeevCloud model API. It is ready in about 9 seconds.
2. **Task.** The script uploads a small JavaScript project whose tests fail, plus an OpenCode config that points at NeevCloud models, and starts OpenCode with the task. The model key is passed only in that process's environment, never written to a file.
3. **Verify.** The script doesn't trust "All tests pass". It checks the test file is unchanged and runs the tests itself. Once the fix is verified, it prints OpenCode's change as a diff and saves the fixed `slugify.js` to `hosted-agent-output/` (change it with `--out`).
4. **Audit.** `agent.audit()` lists every call the script made into the agent, with the program name but never its arguments.
5. **Pause and resume.** The script starts a long-running process, pauses the agent, resumes it, and checks the process is still running: the agent kept its work, without a restart.

## What an agent adds over a plain sandbox

An agent is a sandbox started from an agent template ([Agents](https://docs.ai.neevcloud.com/agentic-studio/overview-1) in the docs). `client.agent_templates.list()` shows the catalogue.

- The coding CLI is already installed and pinned to the template's version, so you only upload a project and a config file.
- The template comes with an egress allow-list for its model providers and package registries, which you can narrow, as here, with `allow_egress=[...]`.
- It has the sandbox lifecycle under `client.agents`: `pause()`, `resume()`, `audit()` and `delete()`, plus `agent.sandbox()` for files, commands and processes.

## Use it in your product

- **Your own repository:** replace the files in `project/` (`PROJECT_FILES`, `TASK` and `TEST_COMMAND` in `hosted_agent.py`) with your project, task and test command.
- **Agents that wait for work:** pause an agent between tasks and resume it when the next one comes in; it keeps its files and running processes.
- **Other coding agents:** pick another template from `client.agent_templates.list()`. The Claude Code and Codex templates need an Anthropic or OpenAI key instead of a NeevCloud one.

## Good to know

- The audit trail shows what the script did in the agent, not what OpenCode did inside its own process; the run's output shows those steps from OpenCode's events.
- OpenCode's own step limit is 25, and the script stops it after 5 minutes.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`. In our runs `glm-5-2` stopped with a response-format error inside OpenCode.

## Time and cost

About a minute with `glm-4-7` (52 to 64 seconds in our runs). OpenCode took 22 to 35 seconds and 4 to 6 model steps. Pausing took 4 to 7 seconds and resuming 6 to 10. You pay for the agent while it runs and for the model tokens OpenCode uses. The agent is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `hosted-agent-` agent from the console.
