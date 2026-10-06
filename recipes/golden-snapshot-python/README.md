# Skip setup with a golden snapshot

Every new sandbox repeats the same slow setup: install packages, build a dataset, start and warm a service. Do it once, take a memory snapshot, and start every worker from that snapshot with the packages installed, the files in place and the service already running with its model in memory.

```text
2. Cold setup, the work every worker would otherwise repeat...
   installed pandas, scikit-learn into /workspace/.venv in 32.0s
   generated data.csv with 200,000 rows in 3.0s
   started the service (proc_49cb2c934f7c32b4bb67676e2678019f); it trained its model and answered after 33.0s
   cold setup: 70.6s from create to a warm service: {"boot_id": "9f7526a2a78486b2", "pid": 24, "served": 1, "rows": 200000, "accuracy": 0.9983}
3. Taking a memory snapshot of the golden sandbox (files, memory and running processes)...
   snapshot 01a10cf8-e87a-73f2-944e-3f89b5aa235b Ready in 16.4s
4. Starting 3 workers from the snapshot, each with no internet access...
   worker 1 golden-w1-9d86a3: service answering 26.0s after create (cold setup took 70.6s, saved 44.6s)
     ok     process: the golden's service answered (boot id 9f7526a2a78486b2, PID 24), proc_49cb2c934f7c32b4bb67676e2678019f running
     ok     memory: request count 2 (was 1 at the snapshot, +1 for this request), model trained in the golden still loaded (accuracy 0.9983)
     ok     files: data.csv has 200,000 rows
     ok     packages: pandas 3.0.6, scikit-learn 1.9.1 import in a new process
     ok     network: egress deny_all, as this worker was created (the golden's allow-list is not carried over)
   golden-w1-9d86a3 deleted.
   ...
Every worker started from the golden snapshot: 25.2s on average instead of 70.6s of cold setup, 45.4s saved per worker.
   Snapshot 01a10cf8-e87a-73f2-944e-3f89b5aa235b deleted.
   golden-src-9d86a3 deleted.
```

## What you need

- Python 3.11 or later
- A NeevCloud account with an API key from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key)) with Resource Type **Sandboxes** (`NEEV_API_KEY`)
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

No model key: this recipe does not call a model.

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python golden_snapshot.py
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

The script exits 0 only when every worker passes every check. To build the golden once and reuse it later, run with `--keep-snapshot`, then pass the printed id to `python golden_snapshot.py --snapshot <id>`: that run skips the build and only starts workers.

## How it works

1. `client.sandboxes.create({"name": ...}, allow_egress=["pypi.org", "files.pythonhosted.org"])` starts the golden sandbox, which can reach PyPI and nothing else.
2. The cold setup runs in it: a virtualenv with `pandas` and `scikit-learn` from PyPI, a 200,000-row dataset (`app/make_data.py`), and a small service (`app/server.py`) started with `sandbox.processes.start(...)`. The service trains a model at start-up and keeps it in memory; it answers with a boot id drawn when it starts, its PID and a request count that lives only in its memory. The script times all of this, from the create call to the service's first answer, and writes it with the service's answer to `golden.json`.
3. `sandbox.snapshot()` takes a memory snapshot. The script polls `client.sandboxes.get_snapshot(id)` while it is `Pending` or `Running` and stops unless it becomes `Ready`.
4. Each worker is a new sandbox created from the snapshot: `client.sandboxes.create({"name": ..., "restore": snapshot_id, "egress": {"mode": "deny_all"}})`. The script times it from the create call to the service's first answer and checks it against `golden.json`: the same boot id and the original process still `running`; a request count of one more than at the snapshot, which a restarted service could not show; the dataset; the packages importing in a new process; and the worker's egress policy.
5. Workers run one at a time and each is deleted after its checks, so the run holds at most two sandboxes. Then the script deletes the snapshot with `client.sandboxes.delete_snapshot(id)` and the golden sandbox.

## What a worker gets from the snapshot

Checked on every worker in our runs:

- **Files**: everything in `/workspace`, including the virtualenv and the dataset.
- **Memory and running processes**: the service that was running in the golden is running in each worker with the same PID, boot id and trained model, and `sandbox.processes.get(id)` finds it under the id the golden gave it. Each worker gets its own copy: every worker's first request showed a count of 2, never 3 or 4.
- **Not the network policy**: a worker gets the egress policy it is created with, `deny_all` unless you say otherwise, not the golden's allow-list. In a separate check, a worker created from the snapshot with no egress setting could not reach `pypi.org`, and one created with `allow_egress=["pypi.org", "files.pythonhosted.org"]` could. Here the workers deliberately get no internet: the installs already happened.

The API also requires a worker to have the same sizing and region as the golden it came from.

## How long the snapshot is kept, and what it costs

A snapshot belongs to the sandbox it was taken from, and deleting the golden sandbox deletes its snapshot as well, so to reuse a snapshot you keep its golden sandbox. That is what `--keep-snapshot` does, and the script prints the sandbox's name. The snapshot itself showed no expiry (`expires_at` was null). NeevCloud's documentation says sandboxes are billed for the time they are `Ready`, so a kept golden sandbox is billed while it runs.

`--snapshot <id>` never deletes a snapshot it was given, or its sandbox. Delete them yourself when you are done: `client.sandboxes.delete_snapshot(id)`, then delete the golden sandbox (the name starts with `golden-src-`) from the console or the SDK.

## Time and cost

About 3 minutes end to end, or about 1.5 minutes with `--snapshot`. In our runs the cold setup took 70.6 to 76.1 seconds, the snapshot was Ready in 16.4 to 18.4 seconds, and a worker's service answered 21.4 to 34.2 seconds after the create call, 44 to 51 seconds faster on average than setting it up again. Starting from a snapshot is not instant: the saving is the setup you skip, so it grows with your setup. You pay for each sandbox while it runs: the golden for the whole run, about 3 minutes, and each worker for under a minute.

## Cleanup

Every worker is deleted after its checks, and the snapshot and the golden sandbox are deleted when the script ends, fails or you press `Ctrl+C`, unless you passed `--keep-snapshot`. If the process is killed outright, delete any leftover `golden-` sandbox from the console; its snapshot goes with it.
