# One MCP URL, a crew of isolated agents

Point several agents at one MCP server without telling them apart and they share one machine and one identity: one agent can overwrite another's work, and afterwards nobody can tell who did what. Here a planner, a coder and a tester connect to the same NeevCloud Sandbox MCP URL, but each gets its own sandbox and its own API key, and the audit trail shows which agent did each thing.

## What you need

- Python 3.11 or later
- A NeevCloud account with API keys from **Account > API Keys**:
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)
  - optionally, one more Sandboxes key per agent: `PLANNER_API_KEY`, `CODER_API_KEY`, `TESTER_API_KEY`
- Your organization and project IDs (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python crew.py
```

By default the crew builds a `slugify(text)` function. Pass your own small task as an argument, for example `python crew.py "a function that converts Roman numerals to integers"`.

Each agent key falls back to `NEEV_API_KEY`. With one key the crew still works and every agent is still confined to its own sandbox, but every audit record shows the same credential. Create a key per agent and export `PLANNER_API_KEY`, `CODER_API_KEY` and `TESTER_API_KEY` to see each action attributed to the agent that made it. The script checks each of these keys before creating anything and names any key that is rejected.

## How it works

The script and the agents hold different powers. The script owns the sandboxes and moves files between them with the SDK; each agent only gets workspace tools over MCP, inside its own sandbox.

1. `client.sandboxes.create({"name": ..., "egress": {"mode": "deny_all"}})` creates three sandboxes, `crew-planner-…`, `crew-coder-…` and `crew-tester-…`, with no internet access. The script creates them with the SDK rather than letting agents call the MCP server's `create_sandbox`, because the SDK sets the egress policy and keeps creation and deletion out of the agents' hands.
2. Each agent opens its own MCP session to the same URL, with `Authorization: Bearer <its key>` and `x-sandbox-name: <its sandbox>`, so every tool call it makes lands in its own sandbox under its own key. It reads the tool list from the server and keeps an allowlist: `fs_write`, `fs_read`, `fs_list` and `exec` for the planner and the coder, and only `fs_read`, `fs_list` and `exec` for the tester, which is given no tool to write the code it judges. A local `finish` tool ends each agent's turn. Calls to any other tool are refused.
3. The planner writes `PLAN.md`: the function's signature, its rules and its edge cases. The script copies it to the coder's sandbox with `sandbox.files.read_text` and `sandbox.files.write`. The coder writes `solution.py` and `test_solution.py`, runs `python3 -m unittest`, and fixes what fails.
4. The script copies the plan, the code and the tests to the tester's sandbox. The tester runs the tests there and reports a verdict. The script accepts a pass only if the tester says so, its last run of the whole suite (`python3 -m unittest`, not a hand-picked test) actually exited 0, and the plan, code and tests in its sandbox are still exactly what was handed over (`exec` could change them).
5. `sandbox.audit()` reads each sandbox's trail, page by page, and the script prints it oldest first, with the credential (`caller_source`) behind every record: the agent's key for its own tool calls, the script's key for the hand-overs. Records arrive a second or two after the call, so the script rereads until the trails stop growing.

The models run on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

The script exits 0 only when the tester's run passed and the audit trails were printed.

## Time and cost

Typically 1 to 2 minutes. Each agent has a step limit and a time limit on its model calls (planner 10 steps and 150 seconds, coder 20 and 300, tester 10 and 150), and a slow model reply is cut off at the limit. You pay for three small sandboxes while the script runs and for the model tokens, about a dozen model calls per run.

## Cleanup

All three sandboxes are deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `crew-` sandbox from the console.
