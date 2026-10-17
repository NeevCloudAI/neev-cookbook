# Disposable integration tests with Docker Compose

Run your integration tests against real Postgres and Redis without Docker on your laptop or CI runner. The script starts an isolated NeevCloud sandbox with Docker in it, brings up a small API with Docker Compose next to a real Postgres and Redis, runs the test suite against them, prints the results, opens the app on a public URL so you can try it in your browser, then throws the whole environment away.

There is also a [Python version](../docker-integration-tests-python).

<!-- run GIF: assets/runs/docker-integration-tests-js.gif, recorded on prod after v0.9.0 -->

<p align="center">
  <img src="../../assets/docker-integration-tests.png" alt="The sample notes app open in a browser on its preview URL, served by the API container with Postgres and Redis behind it" width="640">
</p>

## Run it

You need Node 20.3+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id). You do not need Docker.

```bash
npm install
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

Output:

```
1. Creating a sandbox from the sb-ubuntu-26-04-docker template...
   Ready in 8s: Docker 29.1.3, Compose 2.40.3
2. Uploading the sample app: api/index.html, api/server.ts, compose.yaml, tests/api.test.ts
3. Starting Postgres, Redis and the API (docker compose up --wait)...
   | cache image pulled (11s)
   | api image pulled (23s)
   | db image pulled (46s)
   | cache healthy (57s)
   | db healthy (74s)
   | api healthy (76s)
   All services healthy in 76s
4. Running the integration tests against them...
   | (pass) health check answers [4.97ms]
   | (pass) creating a note stores it in Postgres [86.11ms]
   | (pass) a note without text is rejected [1.14ms]
   | (pass) the second read is served from the Redis cache [80.05ms]
   | (pass) the web page and its data are served [51.82ms]
   | (pass) an unknown note is a 404 [47.68ms]
   6 passed, 0 failed in 6s
5. Opening the app on a public preview URL...
   https://3000-01a1493c-2478-7abe-a3b5-942008449aec-b6c0wl5b.as-south-1.neevsandbox.app
   Keeping it up for 10 minutes. Press Ctrl+C to stop sooner.
6. Tearing it all down (docker compose down -v)...
   Containers and volumes removed in 40s
   Sandbox deleted.
```

The script exits 0 when every test passes and the teardown succeeds, and 1 otherwise, so you can use it as a CI step. The app stays online for 10 minutes after the tests; `npm start -- --keep 0` skips that, which is what you want in CI.

## How it works

1. **Sandbox.** The script starts a sandbox from the `sb-ubuntu-26-04-docker` template, which has Docker and Docker Compose running inside it.
2. **Upload.** It copies the bundled project in `sample-app/` into the sandbox with `sandbox.files.write(...)`: a Compose file, a 40-line notes API (`api/server.ts`) that stores notes in Postgres, caches reads in Redis and serves a one-page web UI (`api/index.html`), and its tests (`tests/api.test.ts`).
3. **Compose up.** `docker compose up -d --wait` pulls the Postgres, Redis and Bun images and returns once every service passes its health check. It runs as a background process with `sandbox.processes.start(...)`, and the script polls its log, printing one line per service as it is pulled and becomes healthy.
4. **Test.** `docker compose run --rm tests` runs the test suite in its own container. The tests make real HTTP calls to the API, which reads and writes the real database and cache.
5. **Preview.** If every test passes, `sandbox.getUrl({ port: 3000 })` gives the API's port a public HTTPS URL. Open it to add notes in the browser; they go through the API into the same Postgres and Redis the tests used.
6. **Tear down.** `docker compose down -v` removes the containers and their data, then the sandbox itself is deleted. The sandbox is deleted even if a step fails or you press `Ctrl+C`.

## Use it in your product

- **Your own project:** point `PROJECT_DIR` in `compose-tests.ts` at your project. Give every service `network_mode: host`, and add a `tests` service under the `test` profile that runs your test command. If your tests are not written for Bun, change `countResults` and `showResult` to read your test runner's output.
- **Integration tests in CI without Docker on the runner:** run the script with `--keep 0` as a CI step. Each run gets a fresh environment, so tests never share state, and nothing is left behind on the runner.
- **Preview environments:** keep the stack up and send the URL to a reviewer, so they can try a branch with real services before it merges.
- **A real stack for a coding agent:** give an agent the sandbox instead of your machine, so it can bring up databases, run the tests and break things safely.

## Good to know

- There is no bridge network: every container must use `network_mode: host`, and services reach each other on `127.0.0.1`. Two services cannot listen on the same port. If you build an image, also set `build: network: host`.
- The sandbox can pull images from Docker Hub and GitHub Container Registry by default. Anything else your build needs, such as a package index for `pip install` or `npm install`, must be added to the sandbox's egress allow-list ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)). The sample API is written for Bun, which has Postgres and Redis clients built in, so it needs no package installs.
- Container memory limits (`--memory`, `mem_limit`) are not enforced; all containers share the sandbox's own memory.
- Each sandbox starts with no images, so every run pulls them again. Smaller images (the `-alpine` variants here) start faster.
- Long steps run as background processes that the script polls, rather than one long `exec` call, so a quiet step such as an image pull never leaves a request waiting with no output.

## Time and cost

Usually 1:30 to 2 minutes: about 8 seconds for the sandbox, 50 seconds to pull the images, 30 seconds for Postgres to initialise and every service to pass its health check, 7 seconds for the tests and 5 to 40 seconds to tear down, plus the time the app stays online (10 minutes unless you pass `--keep`). You pay for the sandbox while it runs. If the process is killed outright, delete any leftover `compose-tests-` sandbox from the console.
