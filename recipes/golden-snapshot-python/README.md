# Skip setup with a golden snapshot

Every new sandbox repeats the same slow setup: install packages, build a dataset, start and warm a service. Do it once, take a memory snapshot, and start every worker from that snapshot with the packages installed, the files in place and the service already running with its model in memory.

<p align="center">
  <img src="../../assets/runs/golden-snapshot-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). No model key: nothing here calls a model.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python golden_snapshot.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The script exits 0 only when every worker passes every check. To build the golden once and reuse it, run with `--keep-snapshot`, then pass the printed id to `python golden_snapshot.py --snapshot <id>` to start only workers.

## How it works

1. **Golden sandbox.** The script starts a sandbox that can reach only PyPI.
2. **Slow setup, once.** It installs pandas and scikit-learn, builds a 200,000-row dataset, and starts a small service that trains a model at start-up and keeps it in memory. It times all of this.
3. **Snapshot.** `sandbox.snapshot()` captures the golden: files, memory and running processes. The script waits until it is `Ready`.
4. **Workers.** Each worker is a new sandbox created from the snapshot with `client.sandboxes.create({..., "restore": snapshot_id})`, here with no internet access. The script checks it has the same running service (same process, same trained model), the dataset and the packages.
5. **Clean up.** Each worker is deleted after its checks; then the snapshot and the golden.

## What a worker gets from the snapshot

- **Files:** everything in `/workspace`, including the virtualenv and the dataset.
- **Memory and running processes:** the golden's service, with the same process ID and trained model. Each worker gets its own copy.
- **Not the network policy:** a worker gets the egress policy it is created with, not the golden's allow-list. Here workers get no internet: the installs already happened.

A worker must have the same sizing and region as the golden it came from.

## Use it in your product

- **Fast-starting agents:** put your toolchain, repositories and warm services in a golden sandbox once, and start each agent from its snapshot.
- **Your own setup:** `set_up()` in `golden_snapshot.py` is the slow part. Replace it with yours; the snapshot and worker code stay the same.
- **Keep a golden around:** `--keep-snapshot` keeps the golden sandbox and its snapshot for later runs.

## How long a snapshot is kept

A snapshot belongs to the sandbox it was taken from, and deleting that sandbox deletes the snapshot too. To reuse a snapshot, keep its golden sandbox, which is billed while it runs. The snapshot itself showed no expiry. `--snapshot <id>` never deletes the snapshot it was given; when you're done, delete it with `client.sandboxes.delete_snapshot(id)`, then delete the golden sandbox (its name starts with `golden-src-`).

## Time and cost

About 3 minutes end to end, or 1.5 minutes with `--snapshot`. In our runs the cold setup took 70 to 76 seconds and a worker's service answered 21 to 34 seconds after its create call, 44 to 51 seconds faster on average. Starting from a snapshot isn't instant: the saving is the setup you skip, so it grows with your setup. You pay for the golden for the whole run and each worker for under a minute. Everything is deleted when the script ends, fails or you press `Ctrl+C`, unless you passed `--keep-snapshot`. If the process is killed outright, delete any leftover `golden-` sandbox from the console.
