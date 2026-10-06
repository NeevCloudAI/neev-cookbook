# AI data analyst

Ask a question about a CSV in plain English. An AI agent answers it by writing and running pandas code in an isolated NeevCloud sandbox with no internet access, and gives you a chart and its findings.

<p align="center">
  <img src="../../assets/runs/ai-data-analyst-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

<p align="center">
  <img src="../../assets/ai-data-analyst.png" alt="Monthly revenue for the top cities, charted by the agent from the bundled sample data" width="640">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python analyst.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

With no arguments it analyses the bundled sample, `data/sales.csv`: a year of synthetic monthly sales for eight Indian cities. The chart is saved as `chart.png` and the findings are printed. To use your own data and question:

```bash
python analyst.py --csv tickets.csv --out tickets.png "Which support channel is slowest to resolve?"
```

## How it works

1. **Install.** The script starts a sandbox that can reach only the Python package index, and installs pandas and matplotlib.
2. **Lock down.** It then removes all internet access with `sandbox.update(...)`, and only then uploads your CSV.
3. **Analyse.** An agent connects to the sandbox over MCP. Its main tool, `run_python`, runs the code the model writes inside the sandbox. It keeps going until it has saved `chart.png` and written its findings.
4. **Download.** The script reads the chart back with `sandbox.files.read(...)`, checks it is a PNG, saves it and prints the findings.
5. **Clean up.** The sandbox, with your CSV in it, is deleted when the script ends, fails or you press `Ctrl+C`.

## Use it in your product

- **"Ask your data" in your app:** call `run()` in `analyst.py` with the user's question and their uploaded file. It saves the chart to the path you give and prints the findings; send both back to your user.
- **Your own analyses:** change `SYSTEM_PROMPT` in `agent.py`, for example to always produce a summary table or to follow your charting style.
- **Other libraries:** add packages to the install step in `analyst.py` (`PIP_INSTALL`); they are installed before internet access is removed.

## Good to know

- Code the model writes cannot open a network connection, so the only data that leaves the sandbox is what that code prints back to the model.
- The model never receives your file as such, only the question, the code it writes and what that code prints, which can include rows of your data.
- The sandbox runs in NeevCloud's `as-south-1` region in Indore, India, so your CSV is stored and processed there. The Model API does not let you choose the region a request is served from.
- The agent gets `run_python`, `fs_list` and `finish`. Any other tool, such as `delete_sandbox` or a bare `exec`, is refused.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=glm-5-2`.

## Time and cost

Usually 75 to 95 seconds: about 30 seconds to install pandas and matplotlib, then 4 to 7 agent steps. The agent is limited to 20 steps and 4 minutes. You pay for the sandbox while it runs and for the model tokens. If the process is killed outright, delete any leftover `data-analyst-` sandbox from the console.
