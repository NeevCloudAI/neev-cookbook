# Bring your own image

Run your exact toolchain image in a NeevCloud sandbox, then snapshot and fork it. The script starts a sandbox from a public Docker image instead of a template, runs a small project's tests there (one fails), forks the sandbox, fixes the bug in the fork only, and shows the original still failing: two independent copies of the same environment, made in seconds.

There is also a [Python version](../bring-your-own-image-python).

<!-- run GIF: assets/runs/bring-your-own-image-js.gif, recorded on prod after v0.9.0 -->

## Run it

You need Node 20.3+, a **Sandboxes** API key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project#find-your-organization-id-and-project-id).

```bash
npm install
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The default image is `docker.io/library/node:22-bookworm`. Pass `--image` to use your own, for example `npm start -- --image ghcr.io/your-org/toolchain:1.4.2`; it needs Node.js 20 or newer for the demo's `node --test`. The script exits 0 only when the fork passes and the original still fails.

Output:

```text
1. Creating a sandbox from docker.io/library/node:22-bookworm...
   Ready in 6.3 s, with Node.js v22.23.3
2. Uploading cart.js, cart.test.js and running the tests...
   pass  adds up price times quantity
   pass  an empty cart costs nothing
   FAIL  takes 10% off (expected 1800, got 1990)
   2 passed, 1 failed
3. Taking a snapshot of the failing state...
   Snapshot Ready in 2.4 s
4. Forking the sandbox...
   Fork byoi-c9341a16-fork Ready in 8.8 s
5. Fixing the bug in the fork only, then running the tests there...
   pass  adds up price times quantity
   pass  an empty cart costs nothing
   pass  takes 10% off
   3 passed, 0 failed
6. Running the tests again in the original, byoi-c9341a16...
   pass  adds up price times quantity
   pass  an empty cart costs nothing
   FAIL  takes 10% off (expected 1800, got 1990)
   2 passed, 1 failed
   Two independent copies of one image: the fork is fixed, the original still has the bug.
   Deleted byoi-c9341a16-fork.
   Deleted byoi-c9341a16.
```

## How it works

1. **Sandbox from your image.** `neev.sandboxes.create({ name, image })` starts a sandbox from a public image instead of a template. The script checks the image has Node.js before going on.
2. **Run the tests.** The project in `project/` (a cart total with a planted discount bug and three tests) is written to `/workspace/cart`, and `sandbox.exec(["node", "--test", ...], { cwd: "/workspace/cart" })` runs the tests there. One fails.
3. **Snapshot.** `sandbox.snapshot({ name, waitUntilReady: true })` saves the failing state and waits until the snapshot is Ready. You could roll back to it later with `sandbox.rollback(snapshot.id)`.
4. **Fork.** `sandbox.fork(name)` makes a second sandbox from the original's current state: same image, same files.
5. **Fix one copy.** The script fixes `cart.js` in the fork only and runs the tests there (they pass), then runs them again in the original (still failing).
6. **Clean up.** Both sandboxes are deleted in `finally`, also when a step fails or you press `Ctrl+C` (the run stops at the next step).

## Use it in your product

- **Your own toolchain:** pass your CI or dev image with `--image`, and replace `TEST_COMMAND` and the files in `project/` with your build or test step.
- **Try changes side by side:** fork once per candidate fix, dependency upgrade or config change, and keep the copy that passes.
- **Set up once, start many:** do the slow setup (clone, install, warm caches) in one sandbox, then fork it for each user or job instead of repeating the setup.
- **Undo:** keep the snapshot and call `sandbox.rollback(snapshotId)` to put a sandbox back to the saved state.

## Good to know

- The image must be public, at most 3 GB, and named in full with an explicit tag or digest, for example `docker.io/library/node:22-bookworm` or `ghcr.io/acme/tool@sha256:...`. A short name like `node:22`, or one with no tag, is refused; the script checks this before creating anything.
- A private or misspelled image fails at create with `Unable to access the container image`.
- Commands start in the image's working directory (`/` for the Node images, which set none), not in `/workspace`. Pass `{ cwd }` to `exec`.
- `/workspace` starts empty, and the files API can only reach paths inside it. Write your project there; files your image ships elsewhere are still there to run.
- Wait for a snapshot to be Ready before you fork: forking while a snapshot is still capturing is refused with `409 A snapshot is already in progress`.
- The sandbox has no internet access, which is why the demo project has no dependencies. To install packages, create the sandbox with an egress allow-list ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).

## Time and cost

About 20 to 30 seconds end to end. The first sandbox from an image you have not used before takes longer while the image is pulled (up to about 30 seconds for `node:22-bookworm`), and about 6 seconds after that. The snapshot is Ready in 2 to 5 seconds and the fork in 4 to 10. You pay for two small sandboxes for those seconds. If the process is killed outright, delete any leftover `byoi-` sandbox from the console.
