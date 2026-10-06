# Best-of-N with fork

One AI attempt at a bug fix is a coin toss. Fork a sandbox three times, let three agents try different approaches in parallel, and keep the first fix that actually passes the tests.

```text
3. Forking best-of-n-35081b7f 3 times...
   3 forks ready in 8.1s, each with the same files
4. Racing 3 agents on glm-4-7, one per fork:
   [1] best-of-n-35081b7f-1  temperature 0.2  Make the smallest change that fixes the bug. ...
   [2] best-of-n-35081b7f-2  temperature 0.7  Run the tests first and reason from each failing assertion ...
   [3] best-of-n-35081b7f-3  temperature 1.0  Reproduce the bug with a short `python3 -c` command ...
   ...
   [3] step 5: exec python3 -c from scheduler.intervals import merge; print(merge([(60, 300), (120, 180)]))
   [3] step 6: fs_write project/scheduler/intervals.py
   [3] step 7: exec python3 -m unittest
   [3] step 8: finish (tests pass)
   [3] passed after 21s: Fixed the merge function to use max() when combining interval end times, ...
5. Results:
   best-of-n-35081b7f-1  temperature 0.2  cancelled after 21s
   best-of-n-35081b7f-2  temperature 0.7  cancelled after 21s
   best-of-n-35081b7f-3  temperature 1.0  passed after 21s
Winner: best-of-n-35081b7f-3 (tests verified by the script). Its change:
--- a/scheduler/intervals.py
+++ b/scheduler/intervals.py
@@ -17,7 +17,7 @@
     merged = []
     for start, end in sorted(intervals):
         if merged and start <= merged[-1][1]:
-            merged[-1][1] = end
+            merged[-1][1] = max(merged[-1][1], end)
         else:
             merged.append([start, end])
   Deleted 4 sandboxes (the base and its forks).
```

## What you need

- Python 3.11 or later
- A NeevCloud account with two API keys from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key)):
  - one with Resource Type **Sandboxes** (`NEEV_API_KEY`)
  - one with Resource Type **Model API** (`NEEV_MODEL_API_KEY`)
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python best_of_n.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The exit code is 0 only when a fork's fix passed the tests.

## How it works

The bug lives in `fixture/`: a tiny meeting scheduler (`scheduler/`) with a `unittest` suite (`tests/`), 12 tests, 3 of them failing. It is pure Python with no dependencies.

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts the base sandbox with no internet access. `sandbox.files.write(...)` uploads the fixture once, and `sandbox.exec(...)` runs the tests to confirm they fail.
2. `base.fork(name)` three times gives three sandboxes that start with the base's files. A fork snapshots the base first, so a second fork requested while that is still running gets a `409` and the script retries it after half a second. Each fork then needs `fork.wait_until_ready()`. Three forks are typically ready about 8 seconds after the first request.
3. Each fork gets its own agent (`agent.py`), started at the same time with `asyncio`. Each agent uses its own MCP session on the sandbox MCP server, bound to its fork with the `x-sandbox-name` header. The agents differ only in temperature and a one-line strategy hint (`STRATEGIES` in `best_of_n.py`). They get four tools from the server, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`. Any other tool call is refused.
4. When an agent calls `finish`, the script checks the fix itself instead of taking the model's word for it. It writes the original test files back over whatever the agent left, then runs the shipped test modules with `python3 -I`, so a file in the project cannot stand in for the standard `unittest`. A fix passes only on exit code 0, `Ran 12 tests` and a plain `OK` with nothing skipped. If the tests fail, the agent gets the failing output back and keeps going.
5. The first fork to pass wins. The other agents are cancelled, the winner's copies of the package files in `fixture/` are diffed against the originals, and every sandbox, the base and all three forks, is deleted in a `finally` block.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

## Time and cost

A run typically takes 30 to 60 seconds. In our runs a fork passed after 16 to 40 seconds of racing.

Forking multiplies the cost, so here is what one run uses. Four sandboxes run at once, the base and three forks, each for under a minute in a typical run: about 2 to 4 sandbox-minutes. The three agents together made 17 model calls using about 64,000 input and 3,000 output tokens in one measured run (`glm-4-7`). Calls cut off by the cancellation are not in that count.

Each agent is limited to 20 steps and 4 minutes, including its tool calls and test runs. If no fork passes, the worst case is all four sandboxes running for about 4.5 minutes (about 18 sandbox-minutes) and three agents each using their 20 steps. More forks finish sooner on average but cost proportionally more.

## Cleanup

The base sandbox and every fork are deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `best-of-n-` sandbox from the console.
