# Code mode over MCP

An agent that reads 200 log files one tool call at a time is slow, expensive and loses count. Give it one tool that runs a Python program next to the data instead, and it writes a short script that does the counting. This recipe runs both approaches on the same question and files in a NeevCloud sandbox, checks both answers, and compares the cost.

<p align="center">
  <img src="../../assets/runs/code-mode-mcp-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python code_mode.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

Add `--show-code` to print the programs the model wrote, and `--seed N` to generate the same logs again. The script exits 0 only when code mode gave the right answer.

## What you'll see

The question: which 3 customers had the most failed payments last week, and how many each. In a typical run on `glm-4-7`:

| | Tool calls | Code mode |
|---|---|---|
| Model calls | 11 | 3 |
| Input tokens | 277,061 | 36,375 |
| Wall time | 80 s | 19 s |
| Answer | wrong | correct |

With tool calls, the model reads the right 67 files but then has to add up a few hundred events in its head, and miscounts. Every model call also resends the whole conversation, so tokens grow with each file. In code mode the files never enter the conversation: the model writes a program that reads them all and gets back a few lines of totals.

Across 12 runs on `glm-4-7`, tool calls answered wrong every time; code mode answered correctly in 10. With `MODEL=minimax-m3`, both answered correctly, but tool calls took about six times as long and used five times the tokens.

## How it works

1. **Data.** The script starts a sandbox with no internet access and uploads three weeks of synthetic payment logs, half JSON and half CSV, with traps like failed refunds and payments outside the week. It computes the true answer itself.
2. **Tool calls.** An agent connects over MCP with `fs_list` and `fs_read` and answers by reading files.
3. **Code mode.** A second agent gets `fs_list` and one more tool, `run_python`. It saves the model's program to the sandbox and runs it, and only the program's output (up to 4,000 characters) goes back to the model.
4. **Compare.** Both answers are checked against the truth and printed next to model calls, tool calls, tokens, data sent to the model and wall time.

## Use it in your product

- **Give your agent code mode:** copy `RUN_PYTHON` and `run_python()` from `agent.py`. It is built from two standard tools of the sandbox's MCP server, `fs_write` and `exec`, so it works with any agent framework that speaks MCP.
- **Big data, small answers:** use it whenever the agent would otherwise read many files or rows: logs, exports, spreadsheets. The data stays in the sandbox and only results reach the model.
- **Check, don't trust:** like the script here, check an answer you can verify before acting on it. Code mode was right 10 times out of 12, not 12.

## Good to know

- `run_python` counts as one tool call in the table, although it is two MCP calls underneath.
- Each mode is bounded by a step limit and a time budget that also cuts off a slow model or tool call.
- Token counts include only model calls that finished.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`.

## Time and cost

Usually 2 to 3 minutes: tool calls take 1 to 2 minutes, code mode usually under one. A run uses roughly 300,000 to 600,000 model tokens, most of them in tool-calling mode. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `code-mode-` sandbox from the console.
