# Contributing to the NeevCloud Cookbook

Recipes and examples are welcome. A recipe here is a promise to the reader: it runs as written, it
shows a result they can see, and it cleans up after itself. This page is what that takes.

## Recipes and examples

- A **recipe** (`recipes/<name>-python` or `recipes/<name>-js`) solves one real problem end to end and
  shows a visible result: a URL, a chart, a report, a rollback that brings data back.
- An **example** (`examples/<name>`) shows how to use NeevCloud sandboxes from a framework or tool you
  already have.

Each folder is self-contained: its own dependencies, its own tests, no shared helper package.

## What every recipe does

- Checks for every environment variable it needs before creating anything, names the missing ones and
  exits `2`.
- Deletes every sandbox and fork it created in a `finally` block, so a failure or `Ctrl+C` leaves
  nothing behind. Sandbox names carry the recipe's prefix plus a random suffix.
- Bounds every agent loop with a step limit and a wall-clock budget, including inside a slow model call.
- Prints numbered progress lines, so a run reads like a demo, and turns errors into one line instead of
  a traceback.
- Exits `0` only when the visible result was achieved.
- Never prints keys and never writes them to a file, on your machine or in a sandbox. Fixture secrets are dummy values created at
  runtime; never commit a `.env` file.
- Uses NeevCloud models through the OpenAI-compatible endpoint, `glm-4-7` by default, overridable with
  `MODEL`. Agent tools come from the Sandbox MCP server's own tool list, filtered to an allow-list,
  rather than hand-written schemas.

## README

Write every recipe README for a customer deciding whether the recipe fits their product. Use the same
order as the existing recipes:

1. **Title and one or two sentences:** the problem, and what the recipe gives you.
2. **A GIF of a real run**, plus any result it produced, such as a chart, a page or a screenshot.
   Record it with a terminal recorder such as [vhs](https://github.com/charmbracelet/vhs), set the keys
   off camera, and check no key, account ID or local path is visible. Save it as
   `assets/runs/<recipe>.gif`.
3. **Run it:** one line on what you need, then install, export the keys and run. Say what success looks
   like and link the [setup guide](docs/setup.md#windows) for Windows.
4. **How it works:** three to five numbered steps in plain language, naming only the key SDK or MCP call.
   Leave out internals such as paging, retries and field names.
5. **Use it in your product:** a few bullets on what to copy or change, each pointing at a real function
   or constant in the recipe's code.
6. **Good to know:** limits, guarantees and known gaps, one line each.
7. **Time and cost:** measured times, the agent's limits, what you pay for, and the cleanup guarantee
   with the prefix of a leftover sandbox.

Only claim what you observed on a real run. Output in a README or a pull request is pasted, never
written by hand.

## Tests

- Unit tests run with no network and no keys: `pytest -q` for Python, `npm test` for TypeScript.
  Run the Python tests from the recipe's folder, in its own virtualenv:

  ```bash
  python3.12 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements-dev.txt
  pytest -q
  ```

  On Windows, create and activate it with `py -3.12 -m venv .venv` and `.venv\Scripts\Activate.ps1`, as in
  the [setup guide](docs/setup.md#windows).

- Fakes in `tests/fakes.py` (or `test/fakes.ts`) mirror the real SDK and MCP shapes.
- Cover the happy path, cleanup on failure, `Ctrl+C`, the step and time limits, malformed tool
  arguments and server refusals, whichever apply.

## Before you open a pull request

- Run the recipe end to end at least three times with the default model and once with another one.
- Interrupt one run part-way and check that no sandbox with the recipe's prefix is left.
- Paste real output from those runs into the pull request, with keys removed.
- Check the diff for keys, account IDs and internal hostnames.

The pull request template has the checklist. A nightly workflow runs every recipe against NeevCloud,
so add your recipe to the matrix in `.github/workflows/nightly.yml` and its prefix to
`.github/scripts/cleanup_sandboxes.py`.

## Reporting problems

Use the issue templates for a broken recipe or a recipe idea. For security issues, follow
[SECURITY.md](SECURITY.md) instead of filing a public issue. Everyone taking part is expected to follow
the [Code of Conduct](CODE_OF_CONDUCT.md).
