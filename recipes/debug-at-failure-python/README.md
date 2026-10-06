# Debug at the failure point

A long job fails part-way and rerunning it costs minutes and may not even hit the bug again. Snapshot the sandbox the moment it fails, fork it, and let an AI agent investigate the fork while the failed process is still running with its memory intact.

```text
1. Creating a sandbox for the job (no internet access)...
2. Running a 3-stage order pipeline over 240 records as a background process...
   stage parse...
   stage enrich...
   stage aggregate...
   Stage aggregate failed at record 198 after 15s: TypeError: unsupported operand type(s) for +: 'float' and 'str'
   The process is still up, holding the batch and its running totals in memory.
3. Snapshotting the sandbox at the failure point (files, memory and running processes)...
   snapshot 01a10fc1-aa36-7afa-91c3-139b55ec5e6c Ready in 1.3s
4. Forking a debug sandbox from that snapshot (no internet access)...
   debug-fail-329da16f-fork Ready in 4.4s
   ok     process: same pipeline process, PID 12 (was 12), proc_b961f5572653e658ce63c8d865491139 running
   ok     failure: still failed in stage aggregate at record 198: TypeError: unsupported operand type(s) for +: 'float' and 'str'
   ok     memory: /state read 13 times (was 12 before the snapshot, +1 for this read)
   Deleted the failed job's sandbox debug-fail-329da16f: the fork does not need it.
5. Asking glm-4-7 to find the root cause in the fork:
   step 1: fs_list /
   step 2: fs_list (invalid arguments)
   step 3: fs_list .
   step 4: fs_read pipeline.py
   step 5: exec curl -s http://127.0.0.1:8080/state
   step 6: exec curl -s http://127.0.0.1:8080/records/198
   step 7: exec curl -s http://127.0.0.1:8080/records/197
   step 8: exec curl -s http://127.0.0.1:8080/records/199
   step 9: exec curl -s http://127.0.0.1:8080/records/0
   step 10: finish
6. The agent's diagnosis:
   record  ord-5ffb5
   field   amount = "3,011.78"
   cause   The amount field contains a string with a comma thousands separator instead of a numeric value. When the aggregate stage tries to add this to the running total (0.0 + "3,011.78"), Python raises TypeError because you cannot add a float and a string.
7. Checking it against the fault the script injected...
   ok     record: ord-5ffb5 (injected: ord-5ffb5)
   ok     field: amount (injected: amount)
   ok     value: "3,011.78" (injected: '3,011.78')
Root cause found in the fork, from the live process's memory, without rerunning the job.
   Deleted debug-fail-329da16f-fork.
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
python debug_at_failure.py
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

The exit code is 0 only when the fork reproduced the failure and the agent's diagnosis matched the fault the script injected.

## How it works

The job is `app/pipeline.py`, a small order pipeline with three stages: parse, enrich and aggregate. It reads its batch of 240 orders from stdin, so the records exist only in the process's memory, and it serves its progress as JSON on `127.0.0.1:8080` inside the sandbox. When a stage fails, it records the error and keeps serving, the way a real worker might sit in a failed state.

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts the job's sandbox with no internet access. The script builds a batch with one bad order at a random position, uploads `pipeline.py` and starts it with `sandbox.processes.start(["python3", "pipeline.py"], stdin=...)`. It then polls `/state` with `sandbox.exec(["curl", ...])` until the job stops. Stage 3 fails part-way, after about 15 seconds.
2. At the failure, `sandbox.snapshot()` captures the sandbox: files, memory and running processes. The script polls `client.sandboxes.get_snapshot(id)` while it is `Pending` or `Running` and stops unless it becomes `Ready`.
3. `client.sandboxes.create({"name": ..., "restore": snapshot_id, "egress": {"mode": "deny_all"}})` forks a debug sandbox from that snapshot. The script checks it holds the failed job itself: the same PID with the process still `running`, the same failure at the same record, and a `/state` read count one past its last read before the snapshot. That count lives only in the process's memory, so a restarted job could not show it. Then it deletes the job's sandbox. Deleting a sandbox deletes its snapshots too, but in our runs the fork kept working without them.
4. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header set to the fork, so everything it does happens in the fork. It keeps three of the server's tools, `fs_read`, `fs_list` and `exec`, plus a local `finish` that takes the bad record's id, field and value and the cause. It gets no file-write, process or lifecycle tools, and a call to any other tool is refused. `exec` can still run any command, but only inside the fork, and the prompt tells it to investigate without changing anything. It is told only the stage and the error message. In our runs it read `pipeline.py`, asked the live process for `/state` and then for the failing record from memory, and reported in 4 to 10 steps.
5. The script compares the agent's findings with the order it injected and prints each check. The fork is deleted in a `finally` block.

`sandbox.fork(name)` does steps 2 and 3 in one call: it snapshots the sandbox and starts the copy. The recipe takes the snapshot itself so the fork comes from the moment the failure was seen, and the snapshot id is printed for the record.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

## Using it on your own job

Swap the pipeline for your job and the `/state` poll for however your job reports failure: an exit code, a status file or a health endpoint. Keep the process alive on failure (catch the error and wait) so its memory is still there to fork. Point the agent at what your job exposes, such as its logs or a debug endpoint.

## Time and cost

About 40 to 55 seconds end to end with `glm-4-7`. In our runs the job failed after 14 to 16 seconds, the snapshot was Ready in 1.2 to 1.4 seconds and the fork 4.3 to 4.4 seconds after that, and the agent took the rest. The agent made 6 to 10 model calls using 15,000 to 24,000 input and 1,000 to 1,500 output tokens; with `glm-5-2` it made 4 calls using about 9,000 input tokens. The two sandboxes run one after the other with a few seconds of overlap, about 25 seconds each, so a run uses under one sandbox-minute. The agent gets at most 15 steps and 3 minutes, including connecting to the MCP server and its tool calls.

## Cleanup

The job's sandbox is deleted as soon as the fork has reproduced the failure, and the fork when the script ends, fails or you press `Ctrl+C`; every sandbox is deleted in a `finally` block. If the process is killed outright, delete any leftover `debug-fail-` sandbox from the console.
