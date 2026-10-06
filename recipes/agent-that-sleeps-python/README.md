# An agent that sleeps

A long-lived agent spends most of its life waiting for the next batch of work. Pause its sandbox between bursts and it stops using compute; resume it in a few seconds with the same running process and everything that process held in memory. An agent can live for days while running only when there is work.

<p align="center">
  <img src="../../assets/runs/agent-that-sleeps-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python sleepy_agent.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

`--nap 300` keeps the sandbox paused for five minutes instead of 30 seconds. The script exits 0 only when the process that wakes up is proven to be the one that went to sleep.

## How it works

1. **A long-running desk.** The script starts a sandbox with no internet access, set to pause itself if left idle, and starts a "desk" process that keeps the agent's notes in memory only. The desk also reports its process ID, a random boot ID and a heartbeat.
2. **First burst.** A triage agent connects over MCP, reads a batch of customer messages and records one note per message on the desk.
3. **Sleep.** `sandbox.pause()` freezes the sandbox. The script waits until it is `Paused`, then leaves it alone.
4. **Wake.** `sandbox.resume()` wakes it. The script runs a cheap command until it succeeds, rather than trusting the reported phase, which can lag.
5. **Same process.** It compares the desk with the one before the pause: the same process ID and boot ID, the same notes, and a heartbeat that carried on from where it stopped. A restarted desk would have none of these. Then the agent triages a second batch on the same desk.

## Pausing on its own when idle

Every sandbox has idle settings, set at create time with `lifecycle` or later with `sandbox.update_timeout(...)`:

- `idle_timeout_seconds`: how long the sandbox may sit without activity. Leave it out for the account default (15 minutes in our tests), or send 0 for no idle limit.
- `on_idle`: `pause` (the default) or `delete`.
- `max_lifetime_seconds` and `paused_retention_seconds`: a hard limit from creation, and how long a paused sandbox is kept before it is deleted. Both were unset by default in our tests.

Activity means calls into the sandbox (commands, files, processes); `sandbox.keepalive()` resets the idle timer. A process running quietly inside doesn't count. The pause can come some minutes after the timeout, so treat it as a safety net and call `pause()` yourself when the agent is done for now, as this recipe does.

## Use it in your product

- **Assistants that wait for work:** pause the agent's sandbox when its queue is empty and resume it on the next message, a webhook or a schedule.
- **Your own agent:** the desk (`app/desk.py`) stands in for whatever your agent keeps in memory: a loaded model, a warm cache or half-finished work. Everything in the sandbox's memory survives the pause.
- **Your own schedule:** `run()` in `sleepy_agent.py` shows the order of calls to copy: work, `pause()`, wait, `resume()`, wait until a command runs, work again.

## Good to know

- While paused, the sandbox uses no compute and its processes are frozen.
- The heartbeat doesn't count while asleep: it carries on from where it stopped.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=glm-5-2`.

## Time and cost

About a minute with the default 30-second nap. In our runs the sandbox reached `Paused` about 1.3 seconds after `pause()`, and the first command ran 2.4 to 3.5 seconds after `resume()`; how long it slept made no difference. Each burst is limited to 12 agent steps and 2 minutes of model time. You pay for the sandbox while it runs and for the model tokens. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`, including while it is asleep. If the process is killed outright, delete any leftover `sleepy-agent-` sandbox from the console.
