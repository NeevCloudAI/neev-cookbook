# Eval rollouts from one golden snapshot

To compare models, prompts or agent versions fairly, every attempt has to start from exactly the same environment, and nothing one attempt does may leak into the next. This recipe builds a golden sandbox once, snapshots it, and runs every task for every model in a fresh sandbox restored from that snapshot. A deterministic checker grades each attempt.

<p align="center">
  <img src="../../assets/runs/eval-rollouts-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python eval_rollouts.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

It compares `glm-4-7` and `minimax-m3`. Pick other models with `--models`, for example `--models glm-4-7,glm-5-2`, and another results file with `--out` (default `results.json`). The script exits 0 when every rollout ran and was graded (a fail is a result, not an error) and everything it created was deleted.

## How it works

1. **Golden.** The script starts a sandbox with no internet access, writes the task files and starts a small inventory service that keeps its stock only in memory.
2. **Snapshot.** It records the golden state (the service's boot ID, its write count and a hash of every file), then snapshots the sandbox.
3. **Rollouts.** For each task and model, a new sandbox is restored from the snapshot. Before the agent starts, the script checks it matches the golden exactly; a mismatch is an error, not a result.
4. **Agent.** An agent connects to its own rollout sandbox over MCP, with at most 15 steps and 3 minutes. The script counts its steps, time and tokens.
5. **Grade.** After the agent finishes, the task's checker runs inside the rollout sandbox, so the agent never sees it. Then the sandbox is deleted. At the end the script prints a grid of results and writes `results.json`.

## The tasks

| Task | The agent must | The checker |
|---|---|---|
| `restock` | set every item with fewer than 5 units to 20 through the service's API | asks the running service for every item's stock |
| `fix-discount` | fix `order_total` in `app/pricing.py` to match its docstring | prices five orders |
| `slugify` | implement `slugify` in `app/text.py` from its docstring | slugifies six titles, including one with accents |
| `revenue-report` | write `report.json` with the paid revenue per region from `data/orders.csv` | compares every region's total |

`restock` changes the service's memory, and every later rollout still starts with 0 writes: proof that nothing carries over.

## Use it in your product

- **Your own tasks:** add a `Task(id, prompt, checker)` to `TASKS` in `tasks.py`, and any files it needs to `golden/`. A checker defines `check()` returning `(passed, detail)`.
- **Your own environment:** whatever you put in the golden (repositories, databases, running services) every rollout gets, identically.
- **Regression tests for agents:** run the suite on every prompt or model change, and compare `results.json` between runs.

## Good to know

- A rollout gets the golden's files, memory and running processes, but not its network policy: it gets the one it is created with, here `deny_all`.
- A snapshot belongs to the sandbox it was taken from, so the golden stays alive until every rollout finishes and is deleted last.
- A checker imports the agent's code, so it tests the agent's work but is not a defence against an agent that tries to game it.
- Results vary between runs: in ours, each model failed `slugify` in one run and passed everything in the rest, and steps, time and tokens always differed.

## Time and cost

About 2 to 4 minutes for the default 8 rollouts, two at a time, so at most three sandboxes at once. In our runs a rollout matched the golden about 5 seconds after its create call, and an agent took 12 to 160 seconds per task. A run used 50,000 to 125,000 tokens per model. You pay for the golden for the whole run, each rollout while its agent works, and the model tokens. Everything is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `eval-roll-` sandbox from the console.
