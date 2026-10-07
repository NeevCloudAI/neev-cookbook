# Human review gate

An agent changed your code. Before you accept the change, review two things together: **the diff** (what it changed) and **the audit trail** (what it did to get there, including files it only read). Approve, and the changed files are copied out. Reject, and the change is thrown away with the sandbox.

This is the TypeScript version of the [Python recipe](../human-review-gate-python). The project the agent changes is a small Python module, as in the original.

<p align="center">
  <img src="../../assets/runs/human-review-gate-js.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

In this run the agent was asked to add input validation to `signup.py`. It changed `signup.py` and `test_signup.py`, and it also read the `.env` file holding the secrets. The read is flagged in the review before anything leaves the sandbox, and the reviewer approves.

## Run it

You need Node 20.3+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
npm install
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The review prints to the terminal and is saved to `review.md`, then you are asked `Approve this change? [y/N]`. Only `y` or `yes` approves, and with no terminal to answer from the change is rejected. In CI, pass `npm start -- --approve` or `-- --reject` instead of answering.

## How it works

1. **Sandbox.** The script starts a sandbox with no internet access and uploads a small Python project, including a `.env` with fake secrets.
2. **Agent.** An agent connects to the sandbox over MCP and is asked to add validation and tests. It can read, write and run commands, but only inside that sandbox.
3. **Audit trail.** The script reads what the agent did from `sandbox.audit()`: every file it read or wrote and every command it ran.
4. **Diff.** The script reads the changed files back and diffs them against the originals. It flags sensitive reads, such as `.env` or SSH keys, and deletes.
5. **Decision.** On approve, only the changed files are written to `approved/<sandbox name>/`. On reject, nothing is written. Either way the sandbox is deleted.

## Use it in your product

- **Your own project:** replace the files in `PROJECT` in `review-gate.ts`, and pass your task as the first argument: `npm start -- "Add rate limiting to api.py"`.
- **Your own reviewer:** the decision is one function, `decide` in `review-gate.ts`. Swap the terminal prompt for a button in your UI, a pull request comment or a ticket approval.
- **Your own report:** `review.ts` builds the review as Markdown (`toMarkdown`), so you can post it to a pull request or a chat channel as it is.
- **Your own rules:** extend the `SENSITIVE` pattern in `review.ts` to flag the files that matter to you.

## Good to know

- Approved files can only land inside `approved/`. Symlinks, binaries, files over 200 KB and unsafe paths are listed but never written.
- Hidden or direction-changing Unicode characters, which can make code read differently from how it runs, are shown as `?` and the file is marked.
- The audit trail records which file or program was used, never arguments or file contents. It shows that `.env` was read, not what was in it.
- Commands the agent runs over MCP currently show as `exec (program not recorded)`: you see that a command ran, not which one.
- The trail can't be read once the sandbox is deleted, so keep `review.md` if you need the record.
- Every action is recorded under the API key that made it. Give each agent its own key to tell them apart.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`.

## Time and cost

About 35 to 55 seconds and under a minute of sandbox time, plus the model tokens of a short session. The agent is limited to 20 steps and 4 minutes. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `review-gate-js-` sandbox from the console.
