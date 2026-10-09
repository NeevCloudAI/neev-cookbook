# Pull request review in CI

Review every pull request with a model and run its tests, without running the pull request's code on your CI runner. A GitHub Action fetches the pull request into a NeevCloud sandbox, a model reviews the diff, the tests run in the sandbox with only your package registry reachable, and the result lands as one comment on the pull request.

This is the TypeScript version of the [Python recipe](../pr-review-ci-python).

<p align="center">
  <img src="../../assets/runs/pr-review-ci-js.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Node 20.3+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
npm install
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

With no arguments it reviews a [demo pull request](https://github.com/NeevCloudAI/neev-cookbook/pull/53) and prints the comment instead of posting it. The demo adds a discount-code function whose tests pass but which returns `NaN` for an unknown code, and a test that tries to send the CI environment's variable names to `example.org`. The script exits 0 when the tests pass and 1 when they fail, so the check on the pull request goes red with them.

To review one of your own pull requests from your machine:

```bash
export GITHUB_TOKEN=$(gh auth token)
npm start -- --repo your-org/your-repo --pr 42 --dry-run
```

- `--test-cmd`: what to run in the checkout (default `npm ci && npm test`), for example `pnpm install --frozen-lockfile && pnpm test`.
- `--allow`: a host the tests may reach, repeatable (default `registry.npmjs.org`).
- `--dry-run`: print the comment instead of posting it. Without it, the script posts, and `GITHUB_TOKEN` must be able to write pull request comments.

## Add it to your repository

1. Copy `review.ts`, `package.json` and `tsconfig.json` to `.github/pr-review/`, and [`workflow.yml`](workflow.yml) to `.github/workflows/pr-review.yml`.
2. Add the repository secrets `NEEV_API_KEY`, `NEEV_MODEL_API_KEY`, `NEEV_ORG_ID` and `NEEV_PROJECT_ID`.
3. Set `--test-cmd` (and `--allow`) in the workflow to match your project, and open a pull request.

The workflow checks out only `.github/pr-review/`, from the base branch, so the runner never runs the pull request's code and a pull request cannot change the script that holds the keys. Each push updates the same comment instead of adding a new one.

## How it works

1. **Read the pull request.** On the CI runner, the script reads the pull request's base and head commits from the GitHub API.
2. **Fetch it into a sandbox.** It creates a sandbox that can reach only `github.com` and your registry, and fetches both commits there. `GITHUB_TOKEN` is passed to that one `git` command as a header and is never written to disk in the sandbox.
3. **Close GitHub.** `sandbox.update({ egress_remove: ... })` takes `github.com` off the allow-list before any of the pull request's code runs.
4. **Review.** The diff goes to a NeevCloud model through the OpenAI-compatible API, from the runner. The model key never enters the sandbox.
5. **Test and comment.** `sandbox.exec(cmd, { stream: true })` runs the test command and streams its output to the job log. The review, the result and the end of the output are posted as one comment, and the sandbox is deleted.

## Use it in your product

- **Your stack:** change `DEFAULT_TEST_CMD` and `DEFAULT_REGISTRIES` in `review.ts` instead of passing flags.
- **Your review style:** edit `REVIEW_PROMPT`, for example to check your team's conventions or to answer in a fixed format.
- **More room:** raise `SANDBOX_RESOURCES` for builds that need more than 1 vCPU and 2 GB of memory, and `TEST_TIMEOUT_MS` for test suites that run longer than 10 minutes.
- **Block on the review too:** the exit code follows the tests only; make `run()` return 1 when the review flags a problem if you want the check to fail on it.

## Good to know

- Pull requests from forks get a read-only `GITHUB_TOKEN` and no repository secrets under `pull_request`, so the job fails at the environment check. Run it on pull requests from branches in your repository, or approve fork runs first.
- GitHub takes the workflow file itself from the pull request, so anyone who can push a branch can change it. The sandbox protects your runner from the pull request's code, not your repository from its collaborators.
- Diffs over 60,000 characters are cut, and the review says so.
- The pull request's code is untrusted input to the model too. The prompt tells the model to ignore instructions in the diff, and `@mentions` in the review are neutralised, but treat the review as advice.
- A test command that prints nothing for 60 seconds is stopped. Most test runners print as they go.
- Tests that need other hosts, such as a database or `api.github.com`, need `--allow` for each one. Hosts match by exact name, with no wildcards.

## Time and cost

Under a minute for the demo (41 to 47 seconds on real runs): 10 to 15 seconds to create the sandbox and fetch, 30 seconds for the review and a few seconds for the tests. You pay for sandbox time (1 vCPU, 2 GB) and one model call per run. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`; if the runner is killed outright, the sandbox deletes itself after 30 minutes. A leftover sandbox's name starts with `pr-review-`.
