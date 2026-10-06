# Eval rollouts from one golden snapshot

To compare models (or prompts, or agent versions) on a set of tasks, every rollout has to start from exactly the same environment, and nothing one rollout does may reach the next. This recipe builds a golden sandbox once, with task files and a service running with its state in memory, takes a memory snapshot, and runs every task for every model in a new sandbox created from that snapshot. Each rollout is graded by a deterministic checker and deleted.

```text
3. Taking a memory snapshot of the golden (files, memory and running processes)...
   snapshot 01a10ffd-aad2-7668-8fef-b5227e0a7e0b Ready in 1.3s
4. Running 8 rollouts (4 tasks x 2 models), 2 at a time, each in a new sandbox from the snapshot...
   [restock / glm-4-7] eval-roll-8d5c36-r1 ready in 5.2s, same as the golden: service boot caf3672d2b228bec, 0 writes, files d2b7131482c9
   [restock / glm-4-7] step 1: fs_read /workspace/SERVICE.md
   [restock / glm-4-7] step 2: exec curl -s http://127.0.0.1:8000/items
   [restock / glm-4-7] step 3: exec curl -s -X PUT -H Content-Type: application/json -d {"stock": 20} http://127....
   ...
   [restock / glm-4-7] PASS in 7 steps, 16.0s (finished): service holds the right stock for all 8 items
   [restock / glm-4-7] left behind 3 service writes, files unchanged; deleting it so no other rollout sees them
   eval-roll-8d5c36-r1 deleted.
   [fix-discount / glm-4-7] eval-roll-8d5c36-r3 ready in 5.1s, same as the golden: service boot caf3672d2b228bec, 0 writes, files d2b7131482c9
   ...
   [slugify / minimax-m3] FAIL in 7 steps, 59.8s (replied without a tool call): NotImplementedError: 
   ...
5. Results:
   task            model         result   steps  seconds   tokens
   restock         glm-4-7       pass         7     16.0   16,513
   restock         minimax-m3    pass         8     25.9   22,431
   fix-discount    glm-4-7       pass         5     12.6   10,738
   fix-discount    minimax-m3    pass         9     23.1   30,801
   slugify         glm-4-7       pass         7     19.9   15,458
   slugify         minimax-m3    fail         7     59.8   22,512
   revenue-report  glm-4-7       pass         5     15.5   11,321
   revenue-report  minimax-m3    pass         5     13.4   14,201
   glm-4-7: 4 of 4 passed, 24 steps, 64.0s, 54,030 tokens
   minimax-m3: 3 of 4 passed, 29 steps, 122.2s, 89,945 tokens
6. Wrote results.json.
   Snapshot 01a10ffd-aad2-7668-8fef-b5227e0a7e0b deleted.
   eval-roll-8d5c36-golden deleted.
```

In this run `minimax-m3` read the files for `slugify`, then answered in text without writing the function: a fail, graded like any other result. In another run `glm-4-7` failed `slugify` by keeping accented letters (`'café-déjà-vu'`, where the docstring allows only ASCII). In the rest of our runs both models passed all four tasks, and the steps, time and tokens still differed from run to run.

## What you need

- Python 3.11 or later
- A NeevCloud account with two API keys from **Account > API Keys**:
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)
- Your organization and project IDs (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python eval_rollouts.py
```

It compares `glm-4-7` and `minimax-m3`, both served by NeevCloud. Pick other models with `--models`, for example `--models glm-4-7,glm-5-2`, and another results file with `--out`. The script exits 0 when every rollout ran and was graded (a fail is a result, not an error) and everything it created was deleted.

## How it works

1. `client.sandboxes.create({"name": ..., "egress": {"mode": "deny_all"}})` starts the golden sandbox with no internet access. The script writes the files in `golden/` into it (two Python modules with work to do, a CSV, the inventory data and the service's API notes in `SERVICE.md`) and starts `inventory_service.py` with `sandbox.processes.start(...)`. The service loads its stock once and from then on keeps it only in memory.
2. The script records the golden state: the service's boot id (drawn when the process starts), its write count, and a hash of every file in the workspace. Then `sandbox.snapshot()` takes a memory snapshot and the script polls `client.sandboxes.get_snapshot(id)` until it is `Ready`.
3. For every task and model, `client.sandboxes.create({"name": ..., "restore": snapshot_id, "egress": {"mode": "deny_all"}})` creates a rollout sandbox from the snapshot. Before the agent starts, the script checks that it matches the golden state: the same service process (a restarted one would have a new boot id), no writes, the same files. A rollout that does not match is an error, not a result.
4. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header of its rollout sandbox, so it can only touch that sandbox. It gets four tools from the server, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`, at most 15 steps and 3 minutes; every model call and tool call gets only the time left. The script counts its steps, time and tokens.
5. The script grades the result with the task's checker from `tasks.py`, which it runs in the rollout sandbox with `sandbox.exec(["python3", "-I", "-"], stdin=checker)` only after the agent has finished, so the agent never sees it. It reports what the agent left behind, deletes the rollout sandbox and starts the next one. Two rollouts run at a time, so the run holds at most three sandboxes. At the end it prints the grid, writes `results.json`, and deletes the snapshot and then the golden sandbox.

## The tasks

| Task | The agent must | The checker |
|---|---|---|
| `restock` | set every item with fewer than 5 units to 20 through the service's API | asks the running service for every item's stock |
| `fix-discount` | fix `order_total` in `app/pricing.py` to match its docstring | prices five orders |
| `slugify` | implement `slugify` in `app/text.py` from its docstring | slugifies six titles, including one with accents |
| `revenue-report` | write `report.json` with the paid revenue per region from `data/orders.csv` | compares every region's total |

`restock` runs first on purpose: each of its rollouts changes the service's memory, and every rollout after it still starts with 0 writes. Editing `data/inventory.json` instead of calling the API fails, because the checker asks the running service.

To add a task, add a `Task(id, prompt, checker)` to `TASKS` in `tasks.py` and any files it needs to `golden/`. A checker defines `check()` returning `(passed, detail)`. It runs inside the rollout sandbox and imports the agent's code, so it is a test of the agent's work, not a defence against an agent that tries to game it.

## Why every rollout gets the same start

A rollout sandbox gets the golden's files, memory and running processes from the snapshot: in our runs every rollout found the golden's service process (same boot id) with 0 writes and the same file hash, including the ones that started right after a `restock` rollout had changed the service three times in its own sandbox. It gets the egress policy it is created with, here `deny_all`, not the golden's.

A snapshot belongs to the sandbox it was taken from, and deleting that sandbox deletes the snapshot. So the golden stays alive until every rollout has finished, and is deleted last.

## Time and cost

About 2 to 4 minutes for the default 8 rollouts. In our runs the snapshot was `Ready` in about 1.2 seconds, a rollout sandbox matched the golden about 5 seconds after its create call, and an agent took 12 to 160 seconds per task (one slow model reply accounts for the top of that range). A run used 50,000 to 90,000 tokens for `glm-4-7` and 75,000 to 125,000 for `minimax-m3`. You pay for the golden sandbox for the whole run, each rollout sandbox for as long as its agent works plus about 10 seconds, and the model tokens.

The `seconds` column is the agent's own time, from its first model call to its last, so sandbox start-up does not count against a model.

## Cleanup

Each rollout sandbox is deleted as soon as it is graded, and the snapshot and the golden sandbox are deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `eval-roll-` sandbox from the console; the snapshot goes with the golden.
