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

Every recipe README follows the same order:

1. One sentence on the problem.
2. A screenshot or a block of real output.
3. What you need: Python 3.11+ or Node 20.3+, and the keys (Resource Type **Sandboxes** for
   `NEEV_API_KEY`, **Model API** for `NEEV_MODEL_API_KEY`).
4. Run it: install, export the keys, run.
5. How it works: about five numbered steps naming the SDK and MCP calls.
6. Time and cost, measured.
7. The cleanup guarantee.

Only claim what you observed on a real run. Output in a README or a pull request is pasted, never
written by hand.

## Tests

- Unit tests run with no network and no keys: `pytest -q` for Python, `npm test` for TypeScript.
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
