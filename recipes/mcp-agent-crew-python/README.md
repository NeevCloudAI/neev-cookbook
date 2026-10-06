# One MCP URL, a crew of isolated agents

Point several agents at one MCP server and, by default, they share one machine and one identity: one can overwrite another's work, and afterwards nobody can tell who did what. Here a planner, a coder and a tester all use the same NeevCloud Sandbox MCP URL, but each gets its own sandbox and its own API key, and the audit trail shows which agent did what.

<p align="center">
  <img src="../../assets/runs/mcp-agent-crew-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python crew.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

By default the crew builds a `slugify(text)` function. Pass your own small task as an argument: `python crew.py "a function that converts Roman numerals to integers"`.

When the tester's run passes, the crew's work is saved on your machine in `crew-output/<run id>/`: the planner's `PLAN.md`, and the coder's `solution.py` and `test_solution.py`. Run `python3 -m unittest` in that folder to check it yourself. Change the folder with `--out`.

With one key, every agent still works in its own sandbox, but the audit trail shows the same credential for all of them. To see each action attributed to its agent, create one more **Sandboxes** key per agent and export `PLANNER_API_KEY`, `CODER_API_KEY` and `TESTER_API_KEY`.

## How it works

1. **Sandboxes.** The script creates three sandboxes with no internet access, one per agent.
2. **One URL, separate sessions.** Each agent connects to the same MCP URL with its own key and its own `x-sandbox-name` header, so everything it does lands in its own sandbox, under its own key.
3. **Plan and code.** The planner writes `PLAN.md`. The script copies it to the coder, who writes `solution.py` and its tests and runs them until they pass.
4. **Test.** The script copies the plan, code and tests to the tester, who runs the full suite and gives a verdict. The tester has no tool to write files, so it can't change what it judges.
5. **Save and audit.** If the tests passed, the script saves the plan, code and tests the tester judged to `crew-output/`. It prints each sandbox's audit trail, with the key behind every action: the agent's key for its own work, the script's key for the hand-overs.

## Use it in your product

- **Your own roles:** each agent is a `Role` in `ROLES` in `crew.py`: its prompt, its tools, its key and its limits. Add a reviewer, swap the tester for a security checker, or change the prompts.
- **Your own hand-offs:** the script, not the agents, moves files between sandboxes (`_hand_over`). Keep it that way so one agent can never reach into another's machine.
- **Least privilege:** give each role only the tools it needs, as the tester here gets no write tool. A call to any tool outside a role's list is refused.
- **Accountability:** one key per agent, or per customer, turns the audit trail into a record of who did what.

## Good to know

- The script creates and deletes the sandboxes itself, rather than letting agents call the MCP server's `create_sandbox`, so it sets the network policy and agents can't create or delete machines.
- A pass counts only if the tester says so, its last run of the whole suite exited 0, and the files in its sandbox are still exactly what it was given.
- Before creating anything, the script checks every agent key and names any that is rejected.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=glm-5-2`.

## Time and cost

Usually 1 to 2 minutes and about a dozen model calls. Each agent has its own limits: planner 10 steps and 150 seconds, coder 20 and 300, tester 10 and 150. You pay for three small sandboxes while the script runs, plus the model tokens. All three are deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `crew-` sandbox from the console.
