# An agent that sleeps

A long-lived agent spends most of its life waiting for the next batch of work. Pause its sandbox between bursts and it stops using compute, then resume it in a few seconds with the same running process and everything that process held in memory, so an agent can live for days while running only when there is work.

```text
3. Burst 1: asking glm-4-7 to triage 4 new messages...
   step 1: fs_read inbox/batch-1.txt
   step 2: exec python3 desk.py add bug: Getting blank page after logging in on Firefox since this morning
   ...
4. Pausing the sandbox...
   Paused 1.3s after pause(): phase Paused, replicas 0. Its processes are frozen; nothing runs while it sleeps.
5. Sleeping 30s without touching the sandbox...
6. Resuming the sandbox...
   resume() returned phase Pending; the first command ran 3.5s after resume()
7. Checking it is the same process...
   ok     process: PID 12 (was 12), boot ID 51150d1b (was 51150d1b), desk running
   ok     memory: 4 notes from before the pause, still held in memory
   ok     heartbeat: 10 -> 12 ticks, 2 in the 35s the script was away (it continued, and did not count while asleep)
8. Burst 2: asking glm-4-7 to triage 4 more messages...
   ...
   the desk now holds 8 notes, all in the memory of PID 12
Proven: the sandbox slept, woke up with the same process and its memory, and the agent carried on.
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
python sleepy_agent.py
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

`--nap 300` keeps the sandbox paused for five minutes instead of 30 seconds. The script exits 0 only when the process that wakes up is proven to be the one that went to sleep.

## How it works

1. `client.sandboxes.create({..., "egress": {"mode": "deny_all"}, "lifecycle": {"idle_timeout_seconds": 300, "on_idle": "pause"}})` starts an isolated Linux machine with no internet access, set to pause itself if it is left idle.
2. `sandbox.processes.start(["python3", "desk.py"])` starts the agent's desk (`app/desk.py`), a long-running process that keeps the agent's notes in memory only. It also reports its PID, a boot ID it picks at random when it starts, and a heartbeat that counts the seconds it has been running.
3. The triage agent (`agent.py`) gets a batch of customer messages. It connects to the sandbox MCP server with the `x-sandbox-name` header and keeps three of the server's tools, `fs_read`, `fs_list` and `exec`, plus a local `finish`; it reads the batch and records one note per message with `python3 desk.py add "<label>: <summary>"`. Pause, resume and delete stay with the script, and a call to any other tool is refused.
4. `sandbox.pause()` puts the sandbox to sleep. The script polls the sandbox record with `sandbox.refresh()` until the phase is `Paused` with 0 replicas, then waits without making any call into the sandbox.
5. `sandbox.resume()` wakes it. `resume()` returns while the sandbox is still starting, and the phase it reports can lag behind, so the script runs a cheap command (`true`) until it succeeds rather than trusting the phase. It then compares the desk with the one before the pause: the same PID and boot ID, the same notes, and a heartbeat that carried on from where it stopped. A restarted desk would have a new boot ID, a heartbeat counting from 0 and no notes. Then the agent triages a second batch on the same desk.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=glm-5-2`.

## Pausing on its own when idle

Every sandbox has idle settings, set at create time with `lifecycle` or changed later with `sandbox.update_timeout(...)`:

- `idle_timeout_seconds`: how long the sandbox may sit without activity. Leave it out to use the account default, or send 0 for no idle limit. On our account the default was 15 minutes: a sandbox created without `lifecycle` reported `idle_expires_at` 15 minutes after its creation.
- `on_idle`: `pause` (the default) or `delete`.
- `max_lifetime_seconds` and `paused_retention_seconds`: a hard limit from creation, and how long a paused sandbox is kept before it is deleted. Both were unset by default on our account (`hard_expires_at` was null).

Activity means calls into the sandbox (commands, files, processes); `sandbox.keepalive()` resets the idle timer without doing anything. A process running quietly inside it does not count. The idle check is not a timer you can schedule work around: the pause can come some minutes after the timeout. Treat the idle timeout as a safety net, and call `pause()` yourself when you know the agent is done for now, as this recipe does.

## Time and cost

About a minute end to end with `glm-4-7` and the default 30-second nap. In our runs the sandbox reached `Paused` about 1.3 seconds after `pause()`, and the first command ran 2.4 to 3.5 seconds after `resume()`; how long it slept made no difference (30 to 60 seconds tested). Each burst gets at most 12 agent steps and 2 minutes of model time. You pay for the sandbox while it runs and for the model tokens the agent uses. While it is paused, it uses no compute and its processes are frozen.

## Cleanup

The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, including while it is asleep. If the process is killed outright, delete any leftover `sleepy-agent-` sandbox from the console; its idle setting may pause it before you do, but do not count on that.
