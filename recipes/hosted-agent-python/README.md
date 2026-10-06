# Hosted coding agent

Run a coding agent on NeevCloud instead of your laptop, give it a task from a script, and check its work yourself. This recipe starts OpenCode from a ready-made agent template, has it fix a failing test suite with a NeevCloud model, verifies the fix, prints the audit trail, then pauses and resumes the agent to show it keeps its work.

```text
1. Creating an agent from the opencode template (it can reach only the NeevCloud model API)...
   hosted-agent-a1c05545 Ready in 9s
2. Uploading a small project whose tests fail...
   node --test: 0 of 4 tests pass
3. Asking OpenCode (glm-4-7) to: "The tests in slugify.test.js fail. Fix slugify.js so that `node --test slugify.test.js` passes. Do not change the tests. Run the tests to check your fix."
   | read slugify.js
   | read slugify.test.js
   | edit slugify.js
   | bash node --test slugify.test.js
   | says: All 4 tests pass.
   OpenCode exited with code 0 after 26s: 4 model steps, 30253 tokens in, 159 out
4. Checking the agent's work ourselves...
   ok     the tests are unchanged
   ok     node --test: 4 of 4 tests pass
5. Audit trail: every call this script made into the agent, oldest first
   08:19:16  fs.write       slugify.js                               success
   08:19:16  fs.write       slugify.test.js                          success
   08:19:16  fs.write       opencode.json                            success
   08:19:17  exec           node                                     success
   08:19:17  process.start  opencode                                 success
   08:19:17  process.logs   proc_291f74ced6391db5e594a887aaed811f    success  x9
   08:19:43  process.get    proc_291f74ced6391db5e594a887aaed811f    success
   08:19:43  fs.read        slugify.test.js                          success
   08:19:44  exec           node                                     success
   17 records, made under API key xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
6. Starting a long-running process, pausing the agent, then resuming it...
   Paused after 5s
   Ready again after 10s
   ok     the process started before the pause is running: the agent carried on, no restart
   ok     node --test after the resume: 4 of 4 tests pass
Task verified: OpenCode fixed slugify.js, the tests pass, and the agent kept its work through a pause.
   Agent deleted.
```

## What you need

- Python 3.11 or later
- A NeevCloud account with two API keys from **Account > API Keys**:
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`), which OpenCode uses to call the model
- Your organization and project IDs (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python hosted_agent.py
```

The script exits 0 only when every check passes: the test file is unchanged, the tests pass after the agent's fix, and after the resume the agent is the same running machine and the tests still pass.

## What a hosted agent adds over a plain sandbox

An agent is a sandbox started from an agent template. `client.agent_templates.list()` shows the catalogue; this recipe uses `opencode`, which works with NeevCloud models. The Claude Code and Codex CLI templates need an Anthropic or OpenAI key instead.

- The coding CLI is already installed and pinned to the template's version, so the script only uploads a project and a config file.
- The template comes with an egress allow-list for its model providers and package registries. The recipe narrows it to the one host it needs with `allow_egress=["inference.ai.neevcloud.com"]`.
- It has the sandbox lifecycle under `client.agents`: `pause()`, `resume()`, `audit()`, `delete()`, plus `agent.sandbox()` for files, commands and processes.

## How it works

1. `client.agents.create({"agent_template": "opencode", ...}, allow_egress=[...])` starts the agent and `agent.wait_until_ready()` waits for it, about 9 seconds in our runs.
2. `machine = agent.sandbox()` gives the files and processes API. The script uploads `project/slugify.js`, its tests and an `opencode.json` that adds NeevCloud as an OpenAI-compatible provider, then runs `node --test` to show the tests fail.
3. `machine.processes.start(["opencode", "run", "--format", "json", ...], env={"NEEV_MODEL_API_KEY": ...})` runs OpenCode in the background. The model key exists only in that process's environment: `opencode.json` refers to it as `{env:NEEV_MODEL_API_KEY}` and it is never written to a file. The script polls `machine.processes.logs()` and prints one line per OpenCode step from its JSON events. OpenCode's own step limit is 25; after 5 minutes the script kills the process.
4. The script does not trust the agent's "All tests pass". It reads `slugify.test.js` back to check the agent did not edit the tests, and runs `node --test` itself.
5. `agent.audit()` returns the audit trail: one record per call the script made into the agent, with the program name but never its arguments, and the API key it was made under (the script masks its ID). Records arrive a few seconds after the call, so the script waits for its last command to appear. What OpenCode does inside its own process, such as its file edits and its test run, is not in the trail; step 3 shows those from OpenCode's output.
6. Before pausing, the script starts a `sleep 3600` process. After `agent.pause()` and `agent.resume()` it waits for the agent to report `Ready` and checks that the process is still running: a restarted machine would have lost it. Then it runs the tests again.

The model is `glm-4-7` by default. Set `MODEL` to use another NeevCloud model, for example `MODEL=minimax-m3`. In our runs `glm-5-2` stopped with a response-format error inside OpenCode.

## Time and cost

About a minute end to end with `glm-4-7` (52 to 64 seconds in our runs, 76 to 86 with `minimax-m3`). With `glm-4-7` OpenCode took 22 to 35 seconds and 4 to 6 model steps, using 30,000 to 48,000 input tokens and a few hundred output tokens. In our runs pausing took 4 to 7 seconds and resuming 6 to 10. You pay for the agent while it runs and for the model tokens OpenCode uses.

## Cleanup

The agent and everything in it are deleted when the script ends, fails or you press `Ctrl+C`. If you press `Ctrl+C` while the agent is still being created, or the process is killed outright, delete any leftover `hosted-agent-` agent from the console.
