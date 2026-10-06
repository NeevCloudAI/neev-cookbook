# Report generator

Turn a CSV into a finished business report, a PDF and an Excel workbook, without installing a PDF or spreadsheet toolchain on your machine. An AI agent writes and runs the report code inside an isolated NeevCloud sandbox, and the script downloads both files.

<p align="center">
  <img src="../../assets/report-generator.png" alt="Page 1 of the expense report the agent built from the bundled sample data" width="420">
</p>

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
python report.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

With no arguments it reports on the bundled sample, `data/expenses.csv`: 360 rows of synthetic 2025 monthly budget and actual spend for six departments and five cost categories, with a few made-up overruns to find. `report.pdf` and `report.xlsx` are saved in the current directory and the key findings are printed.

To use your own data and brief:

```bash
python report.py --csv sales.csv --out-dir reports "Quarterly sales report by region and product line"
```

## How it works

The script and the agent hold different powers. The script uses the SDK for the lifecycle, the network and the downloads; the agent only gets workspace tools over MCP.

1. `client.sandboxes.create({...}, allow_egress=["pypi.org", "files.pythonhosted.org"])` starts an isolated Linux machine that can reach only the Python package index. `sandbox.exec(["python3", "-m", "pip", "install", ...])` installs `pandas`, `matplotlib`, `openpyxl` and `fpdf2`, which the default template does not include.
2. `sandbox.update({"egress": {"mode": "deny_all"}})` then removes all internet access, before your data is uploaded with `sandbox.files.upload_file(path, "data.csv")`. Code the model writes cannot open a connection to any host.
3. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header, so its session is bound to that one sandbox. It reads the tool list from the server and keeps four tools, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`. Lifecycle tools such as `delete_sandbox` are never offered, and a call to one is refused. The agent writes `build_xlsx.py` and `build_pdf.py`, runs them, and fixes them until they work.
4. `finish` is accepted only when both files pass the script's checks: each must be a regular file of at most 50 MB, `sandbox.files.read()` downloads it, the PDF must have a PDF header and end marker, at least one page and an embedded chart image, and the workbook must open as an XLSX with at least two sheets and at least one formula. Otherwise the agent is told what is wrong and keeps going. The script then saves both files and prints their page, image, sheet and formula counts. The checks use only the Python standard library.
5. `sandbox.delete()` runs in a `finally` block, so the sandbox is removed even if the agent fails or you press `Ctrl+C`.

The checks prove the files are real and have the parts asked for; they do not prove every number is right. The agent is told to put only computed figures in the report and to quote its findings from what its scripts printed, so read the report as a model-written draft. The workbook's formulas are calculated when you open it in Excel, LibreOffice or Google Sheets.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=minimax-m3`.

## Time and cost

Typically 2 to 4.5 minutes end to end with the default model: about 50 seconds to install the packages, then 7 to 22 agent steps. The agent gets at most 25 steps and 7 minutes; if it runs out with both files already passing the checks, you get them without the findings. You pay for the sandbox while it runs and for the model tokens the agent uses.

## Cleanup

The sandbox and everything in it, including the uploaded CSV, are deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `report-gen-` sandbox from the console.
