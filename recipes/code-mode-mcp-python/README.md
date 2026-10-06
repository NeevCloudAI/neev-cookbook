# Code mode over MCP

An agent that answers a question about 200 log files by reading them one tool call at a time is slow, expensive, and loses count. Give it one tool that runs a Python program next to the data instead, and it writes a short script that does the work. This recipe runs both on the same question and the same files in a NeevCloud sandbox, checks both answers, and prints the cost side by side.

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
python code_mode.py
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

Add `--show-code` to print the programs the model wrote, and `--seed N` to generate the same logs again. The script exits `0` only when code mode gave the right answer; the tool-calling result is reported either way.

## What you see

A real run on glm-4-7:

```
1. Creating a sandbox (no internet access)...
2. Uploading 200 payment log files (157 KB, JSON and CSV, seed 101791)...
   Question: Which 3 customers had the most failed payments last week (Monday 2026-09-28 to Sunday 2026-10-04, UTC), and how many failed payments did each have? ...
3. Tool calls: glm-4-7 gets fs_list and fs_read (at most 20 model calls, 180s)...
   call 1: fs_list
   call 2: fs_read x2
   call 3: fs_read x4
   ...
   call 10: fs_read x10
   call 11: finish
4. Code mode: glm-4-7 gets run_python and fs_list (at most 10 model calls, 120s)...
   call 1: fs_list
   call 2: run_python
   call 3: finish
5. Results, same question and same files:
                           tool calls     code mode
   model calls                     11             3
   tool calls                      68             2
   input tokens               277,061        36,375
   output tokens                4,136         1,228
   data sent to model         90.5 KB       39.0 KB
   wall time                    80.3s         19.1s
   answer                       wrong       correct
   Ground truth:        everfresh-dairy 17, orchid-salons 13, nimbus-cloudworks 8
   Tool calls answered: orchid-salons 13, everfresh-dairy 5, saffron-kitchens 5 (answered)
   Code mode answered:  everfresh-dairy 17, orchid-salons 13, nimbus-cloudworks 8 (answered)
Code mode answered correctly with 3 model calls and 37,603 tokens; tool calls used 11 model calls and 281,197 tokens.
   Sandbox deleted.
```

In tool-calling mode the model is smart about it: it lists the folder and reads only the files from the week in question (67 of the 200). But then it has to add up a few hundred events in its head across a long conversation, and it miscounts. Every model call also resends the whole conversation, so input tokens grow with each file read. In code mode the files never enter the conversation: the model writes a program that reads all of them (often after looking at one sample file) and reads back a few lines of totals. Most of the data code mode does send is the directory listing it asked for. A `run_python` call counts as one tool call in the table, although it is two MCP calls underneath.

Results vary between runs. In 12 runs on glm-4-7, tool calls answered wrong every time, using 10 to 19 model calls and roughly 270,000 to 550,000 input tokens. Code mode answered correctly in 10 of the 12, with 3 to 9 model calls, 36,000 to 160,000 input tokens and 19 to 79 seconds, against 63 to 108 seconds for tool calls. In the other two, the model once kept writing broken programs until it ran out of steps, and once gave a wrong count. That is why the script checks the answer instead of trusting it. With `MODEL=minimax-m3`, both modes answered correctly in our runs, but tool calls took about six times as long as code mode and used 327,000 to 355,000 input tokens against 55,000 to 76,000.

## How it works

The script holds the lifecycle and the ground truth; the model only gets tools over MCP.

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts an isolated Linux machine with no internet access.
2. `fixture.py` generates three weeks of synthetic payment logs, half JSON and half CSV, with traps a careless count falls into: failed refunds, and failed payments outside the week. The script computes the true top three itself. The files go up as one archive with `sandbox.files.write` and are unpacked with `sandbox.exec("tar", ...)`.
3. Tool calls: the agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header, reads the tool list, and keeps `fs_list` and `fs_read`, plus a local `finish` that takes the answer.
4. Code mode: the same question on a new MCP session, with `fs_list` and a local `run_python(code)` tool. `run_python` is two MCP calls: `fs_write` saves the program to `.code-mode/snippet_N.py` in the sandbox and `exec` runs it with `python3`. Only its stdout, stderr and exit code (clipped to 4,000 characters) go back to the model.
5. Both answers are compared with the ground truth and printed next to model calls, tool calls, tokens (from the API's usage fields), bytes of tool output sent to the model, and wall time. A tool not offered to a mode is refused, and each mode is bounded by a step limit and a time budget that also cuts off a slow model call or tool call.

`sandbox.delete()` runs in a `finally` block, so the sandbox is removed even if a mode fails or you press `Ctrl+C`.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=minimax-m3`.

## Time and cost

Typically 2 to 3 minutes: tool calls take 1 to 2 minutes, code mode usually under a minute. Tool calls get at most 20 model calls and 180 seconds, code mode 10 model calls and 120 seconds. A run uses roughly 300,000 to 600,000 model tokens, most of them in tool-calling mode. Token counts only include model calls that finished; a call cut off by the time limit is not counted. You pay for the sandbox while it runs and for the model tokens.

## Cleanup

The sandbox and everything in it are deleted when the script ends, fails or you press `Ctrl+C`. If the delete itself fails, the script names the sandbox to remove. If the process is killed outright, delete any leftover `code-mode-` sandbox from the console.
