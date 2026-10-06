# Fix the failing test

Hand an AI agent a repository with a failing test. It fixes the code, never the tests, in an isolated NeevCloud sandbox, and you get back a `fix.patch` the script has proven: the tests pass in a fresh sandbox, the test files are untouched, and the patch applies cleanly. Your repository is only read; applying the patch is up to you.

<p align="center">
  <img src="../../assets/runs/fix-failing-test-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, `git`, a clone of this repository, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python fix_test.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

By default it fixes `fixture/`, a small shop module with two real bugs and four failing tests. To fix your own repository:

```bash
python fix_test.py --repo ~/code/my-lib --test-cmd "python3 -m unittest tests/test_slugs.py" --out ~/my-lib-fix.patch
```

- `--repo`: a git repository, or a folder in one. Only tracked files are uploaded, up to 300 files and 5 MB; untracked files such as `.env` stay on your machine.
- `--test-cmd`: the test command, run in that folder (default `python3 -m unittest`). The sandbox has Python 3 and no internet, so tests must run with the standard library.
- `--out`: where the patch goes (default `./fix.patch`), outside `--repo`. Apply it with `git apply` at the repository's top level.

## How it works

1. **Reproduce.** The script uploads your tracked files to a sandbox with no internet access and runs the tests to confirm they fail.
2. **Fix.** An agent connects to the sandbox over MCP, reads the code, edits it and reruns the tests. It can change only Python source files: test files and every non-Python file are read-only, and it can finish only once the tests pass.
3. **Verify the files.** The script hashes every file in the sandbox. If any read-only file changed by a single byte, the run fails.
4. **Build the patch.** The script reads the changed files back, builds a patch and checks it applies cleanly to your originals with `git apply`.
5. **Verify the fix.** It deletes the agent's sandbox, starts a fresh one with the original files plus only the changed source files, and runs the tests again. Only if they pass does it write `fix.patch`.

## Use it in your product

- **A fix bot for CI:** run `fix_test.py` when a test job fails, with `--test-cmd` set to the failing test, and attach `fix.patch` to the pull request for a person to review.
- **Your own guardrails:** `is_read_only` in `fix_test.py` decides which files the agent may not touch. Extend it to protect migrations, configs or generated code.
- **Projects with dependencies:** the sandbox has no internet, so third-party test dependencies aren't installed. To use them, create the sandbox with an egress allow-list for your package index ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).

## Good to know

- Read-only files: anything under a `test*` or `*tests` folder, `test*.py`, `*_test.py`, `conftest.py`, and every non-Python file. So test data, configs and scripts can't be changed to make the tests pass.
- Files the agent created are listed and left out of the patch, so a fix that depends on them fails the fresh-sandbox run.
- The script exits 0 only when every check passes.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`.

## Time and cost

About 25 to 40 seconds for the bundled example, and under a minute of sandbox time across the two sandboxes, plus the model tokens. The agent is limited to 30 steps and 5 minutes, and each test run to 2 minutes. Every sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `fix-test-` sandbox from the console.
