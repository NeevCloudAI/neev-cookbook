# OpenAI Agents SDK with NeevCloud Sandboxes

An Agents SDK agent with a sandbox as its tool.

`OpenAIChatCompletionsModel` is used rather than the default Responses API, so
this works against any OpenAI-compatible endpoint — including NeevCloud's own
inference API, which is what the commands below point at.

<p align="center">
  <img src="../../assets/runs/openai-agents-sdk-python.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_BASE_URL=https://inference.ai.neevcloud.com/v1
export NEEV_MODEL_API_KEY=...

python sandbox_agent.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The agent writes `primes.py`, runs it, and reports the output.

Output:

````
  sandbox ready: oa-92008635

  agent: Here is exactly what it printed:

```
2
3
5
7
11
13
17
19
23
29
```

  sandbox deleted
````

## Notes

Tracing is disabled — there is no OpenAI account involved, so there is no tracing
backend to report to. Remove `set_tracing_disabled(True)` if you are using
api.openai.com and want traces.
