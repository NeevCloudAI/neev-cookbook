# Best-of-N with fork

One AI attempt at a bug fix is a coin toss. Fork a sandbox three times, let three agents try different approaches in parallel, and keep the first fix that actually passes the tests.

This is the TypeScript version of the [Python recipe](../best-of-n-fork-python). The bug the agents fix is in a small Python package, as in the original.

<p align="center">
  <img src="../../assets/runs/best-of-n-fork-js.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Node 20.3+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
npm install
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The bug lives in `fixture/`: a tiny meeting scheduler with 12 tests, 3 of them failing. The script exits 0 only when a fork's fix passed the tests, and prints the winning change as a diff.

## How it works

1. **Base sandbox.** The script starts a sandbox with no internet access, uploads the project and confirms the tests fail.
2. **Fork.** `base.fork(name)` three times gives three sandboxes that start with the base's files. Three forks are typically ready about 8 seconds after the first request.
3. **Race.** One agent per fork, all at once. Each connects over MCP to its own fork and differs only in temperature and a one-line strategy: smallest change, reason from the failing assertions, or reproduce the bug first.
4. **Verify.** When an agent says it is done, the script puts the original test files back and runs the full suite itself. A fix passes only on a clean `OK` for all 12 tests; otherwise the agent gets the failure and keeps going.
5. **Keep the winner.** The first fork to pass wins, the other agents are cancelled, and the script prints the winner's diff and deletes all four sandboxes.

## Use it in your product

- **Your own bug:** replace `fixture/` with your project and tests. `testCommand()` in `best-of-n.ts` decides how the tests run.
- **Your own strategies:** `STRATEGIES` in `best-of-n.ts` holds each agent's temperature and hint. All three use one model here; giving each its own model is a small change that widens the search further.
- **Any parallel exploration:** forking works for anything where you'd try several paths from the same starting point, such as data migrations, refactors or config changes.

## Good to know

- A fork snapshots the base first, so a second fork requested while that is still running gets a `409`; the script retries after half a second.
- The script restores the original tests before checking, so an agent can't pass by editing them.
- The model is `glm-4-7` by default. Set `MODEL` to try another, for example `MODEL=minimax-m3`.

## Time and cost

Usually 35 to 45 seconds; in our runs a fork passed after 23 to 28 seconds of racing. Forking multiplies the cost: four sandboxes run at once for under a minute (2 to 4 sandbox-minutes), plus the model tokens of three short agent sessions. Each agent is limited to 20 steps and 4 minutes, so the worst case, with no fork passing, is about 18 sandbox-minutes. Every sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `best-of-n-js-` sandbox from the console.
