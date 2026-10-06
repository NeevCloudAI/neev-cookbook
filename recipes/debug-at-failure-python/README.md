# Debug at the failure point

A long job fails part-way. Rerunning it costs minutes and may not even hit the bug again. Instead, snapshot the sandbox the moment it fails, fork it, and let an AI agent investigate the copy while the failed process is still running with its memory intact.

<p align="center">
  <img src="../../assets/runs/debug-at-failure-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python debug_at_failure.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The script exits 0 only when the fork reproduced the failure and the agent's diagnosis matched the fault the script injected.

## How it works

1. **The job.** The script starts a sandbox with no internet access and runs a three-stage order pipeline over 240 records, with one bad record hidden at a random position. The records exist only in the process's memory. Stage 3 fails part-way, and the process stays up, serving its state on a local port.
2. **Snapshot at the failure.** `sandbox.snapshot()` captures files, memory and running processes at that moment.
3. **Fork.** A new sandbox is created from the snapshot. The script checks it holds the failed job itself: the same process, the same failure, and a read counter that lives only in memory. Then it deletes the original sandbox.
4. **Investigate.** An agent connects to the fork over MCP with read-only file tools and `exec`, and is told only the stage and the error. In our runs it read the code, asked the live process for its state and the failing record, and found the cause in 4 to 10 steps.
5. **Check.** The script compares the agent's answer (record, field and value) with the fault it injected.

## Use it in your product

- **Your own jobs:** keep the process alive on failure (catch the error and wait), so its memory is still there to fork. Swap the state poll for however your job reports failure: an exit code, a status file or a health endpoint.
- **Point the agent at what you expose:** logs, a debug endpoint or a state dump. The richer it is, the faster the diagnosis.
- **Debug without touching production:** the agent works in the fork, so the original can be kept, deleted or restarted independently.
- **One call instead of two:** `sandbox.fork(name)` snapshots and starts the copy in one step. This recipe takes the snapshot itself so the fork comes from the exact moment the failure was seen.

## Good to know

- The agent gets `fs_read`, `fs_list` and `exec` from the sandbox's MCP server, plus `finish`. It has no write, process or lifecycle tools, and the prompt tells it not to change anything. `exec` can still run any command, but only inside the fork.
- Deleting a sandbox deletes its snapshots, but in our runs the fork kept working after the original was deleted.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=glm-5-2`.

## Time and cost

About 40 to 55 seconds with `glm-4-7`. In our runs the job failed after 14 to 16 seconds, the snapshot was `Ready` in about 1.3 seconds and the fork about 4.4 seconds later. The two sandboxes overlap only briefly, so a run uses under one sandbox-minute, plus 15,000 to 24,000 input tokens. The agent is limited to 15 steps and 3 minutes. Every sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `debug-fail-` sandbox from the console.
