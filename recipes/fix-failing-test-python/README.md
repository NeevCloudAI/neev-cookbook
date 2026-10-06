# Fix the failing test

Hand an AI agent a repository with a failing test. It fixes the code in an isolated NeevCloud sandbox, and you get back a `fix.patch` that the script has proven: the tests pass, the test files are untouched, and the patch applies cleanly. The script only reads your repository; applying the patch is up to you.

```text
3. Running the tests: python3 -m unittest
   | AssertionError: 67260 != 63720
   |
   | ----------------------------------------------------------------------
   | Ran 8 tests in 0.003s
   |
   | FAILED (failures=4)
4. Asking glm-4-7 to fix the code (2 files read-only: tests and non-Python files)...
   step 1: fs_read /workspace/tests/test_cart.py
   step 1: fs_list /workspace
   step 2: fs_read /workspace/shop/cart.py
   step 3: fs_write /workspace/shop/cart.py
   step 4: exec python3 -m unittest
   step 5: finish (running the tests)
   agent's summary: Fixed bulk_discount_percent to check >= 50 before >= 10, and fixed gst to round halves up using (amount * GST_PERCENT + 50) // 100.
5. Checking the agent's work...
   unchanged: 2 read-only files, byte for byte
   changed: shop/cart.py
   Sandbox deleted.
6. Running the tests in a fresh sandbox: the original files plus only the changed source files...
   | ........
   | ----------------------------------------------------------------------
   | Ran 8 tests in 0.001s
   |
   | OK
7. Wrote fix.patch; it applies cleanly to the original files:

diff --git a/recipes/fix-failing-test-python/fixture/shop/cart.py b/recipes/fix-failing-test-python/fixture/shop/cart.py
--- a/recipes/fix-failing-test-python/fixture/shop/cart.py
+++ b/recipes/fix-failing-test-python/fixture/shop/cart.py
@@ -5,16 +5,16 @@
 
 def bulk_discount_percent(quantity: int) -> int:
     """Percentage off for buying in bulk: 5% from 10 items, 10% from 50 items."""
+    if quantity >= 50:
+        return 10
     if quantity >= 10:
         return 5
-    if quantity >= 50:
-        return 10
     return 0
 
 
 def gst(amount: int) -> int:
     """GST on an amount in paise, rounded to the nearest paisa (a half rounds up)."""
-    return amount * GST_PERCENT // 100
+    return (amount * GST_PERCENT + 50) // 100
 
 
 def order_total(unit_price: int, quantity: int) -> int:

Apply it with:
   cd ~/neev-cookbook && git apply ~/neev-cookbook/recipes/fix-failing-test-python/fix.patch
   Sandbox deleted.
```

## What you need

- Python 3.11 or later, and `git`
- A clone of this repository (the bundled example is read with `git ls-files`)
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
python fix_test.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

By default it fixes `fixture/`, a small shop module with two real bugs and four failing tests. To fix your own repository instead:

```bash
python fix_test.py --repo ~/code/my-lib --test-cmd "python3 -m unittest tests/test_slugs.py" --out ~/my-lib-fix.patch
```

- `--repo` is a git repository, or a folder inside one. Only tracked files are uploaded, as they are in your working tree; untracked files such as `.env` stay on your machine, and symlinks are skipped. The limit is 300 files and 5 MB.
- `--test-cmd` runs in the `--repo` folder inside the sandbox (default `python3 -m unittest`). The sandbox has Python 3 and no internet access, so the tests must run with the standard library: `pytest` and your dependencies are not installed.
- `--out` is where the patch goes (default `./fix.patch`). It must be outside the `--repo` folder. The patch's paths are relative to the git top level, so `git apply` runs there.

The agent may change only Python source files outside the tests. Test files (anything under a `test*` or `*tests` folder, `test*.py`, `*_test.py`, `conftest.py`) and every non-Python file are read-only, so test data, configs and scripts cannot be changed to make the tests pass. The script exits 0 only when every check passes.

## How it works

The script and the agent hold different powers. The script uses the SDK to set up the sandbox and to check the result; the agent only gets workspace tools over MCP.

1. `client.sandboxes.create({"egress": {"mode": "deny_all"}})` starts an isolated Linux machine with no internet access. `sandbox.files.write()` uploads the tracked files, and `sandbox.exec(["sh", "-c", test_cmd])` runs the tests to confirm they fail. If they already pass, there is nothing to fix and the script stops.
2. The agent (`agent.py`) connects to the sandbox MCP server with the `x-sandbox-name` header, so its session is bound to that one sandbox. It reads the tool list from the server and keeps four tools, `fs_write`, `fs_read`, `fs_list` and `exec`, plus a local `finish`. Writing to a read-only file is refused, and `finish` is accepted only after the tests pass.
3. The script does not take the agent's word for it. One `sandbox.exec` hashes every tracked file in the workspace: any read-only file that differs from the original by a single byte, or is gone, fails the run, whatever the agent did to it.
4. The script reads the changed source files back with `sandbox.files.read()` and deletes the agent's sandbox. It then creates a fresh sandbox that the agent never touched, uploads the original files plus only the changed source files, and runs the tests there. New files the agent created are listed and left out, so a fix that depends on them fails here, and so does one that depends on anything else the agent changed in its own sandbox.
5. Before creating the second sandbox, the script builds a unified diff of the changed source files, applies it with `git apply --check` and `git apply` to a temporary copy of the originals on your machine, and checks the result matches the fixed files byte for byte. Once the fresh sandbox passes, it writes `fix.patch`. You apply it with `git apply`.

The model runs on NeevCloud too: `glm-4-7` by default. Set `MODEL` to use a different one, for example `MODEL=minimax-m3` or `MODEL=glm-5-2`.

## Time and cost

About 25 to 40 seconds for the bundled example with `glm-4-7`. The agent gets at most 30 steps and 5 minutes of model time; each command it runs is also bounded by the sandbox's per-call time limit, and each test run by the script by 2 minutes. You pay for the sandbox while it runs, typically under a minute across the two sandboxes, and for the model tokens the agent uses.

## Cleanup

The agent's sandbox is deleted before the second one is created, and every sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `fix-test-` sandbox from the console.
