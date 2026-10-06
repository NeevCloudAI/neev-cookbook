# Run untrusted user code in your SaaS

Your product lets end users run their own Python or JavaScript. This recipe is the backend for that: a small HTTP server that runs each user's code in that user's own NeevCloud sandbox, with no network, hard memory, process and time limits, and the sandbox deleted when the user goes idle.

## What you need

- Node 20 or later
- A NeevCloud API key with Resource Type **Sandboxes** (`NEEV_API_KEY`), from **Account > API Keys** ([how to create one](https://docs.ai.neevcloud.com/getting-started/create-api-key))
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project) (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

No model key is needed: nothing here calls a model.

## Run it

```bash
npm install
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm run demo
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The demo starts the server, sends it ordinary and hostile code as two users, checks that each run was contained, waits for the idle timeout to delete both sandboxes, and asks the API to confirm they are gone. It exits `0` only when every check passed and every sandbox was deleted.

## What you see

From a run on NeevCloud (some lines trimmed):

```
1. Code runner on http://127.0.0.1:58402/run: at most 2 sandboxes, 5s per run, no internet, idle sandboxes deleted after 20s
2. alice (python): ordinary code that saves a private file
   [runner] created code-runner-a04f6fb7 for alice
   exit 0 in 0.5s
   stdout: sum of 1..100 = 5050
   ok: ran normally in alice's own sandbox
...
4. carol (python): a third user while the cap is 2 sandboxes
   HTTP 429: all 2 sandboxes are in use; try again later
   ok: refused with 429: no third sandbox is created
5. alice (python): an infinite loop
   cut off after 5.3s
   stdout: spinning...
   ok: killed at the per-run time limit
6. alice (python): a memory hog (64 MB at a time, up to 4 GB)
   exit 1 in 1.2s
   stdout: 448 MB
   stderr: MemoryError
   ok: MemoryError at the per-process memory cap; the sandbox stayed up
7. alice (python): a fork bomb (every process forks until refused)
   cut off after 5.6s
   stdout: fork refused after 6 forks: Resource temporarily unavailable
   ok: forks refused at the per-run process cap, and every process killed at the time limit
8. bob (node): a memory hog (64 MB buffers, up to 4 GB)
   exit 1 in 2.6s
   stdout: 640 MB
   stderr: code: 'ERR_MEMORY_ALLOCATION_FAILED'
   ok: allocation failed at the per-process memory cap; the sandbox stayed up
9. bob (python): try to read alice's private file
   exit 1 in 0.7s
   stdout: files: []
   stderr: FileNotFoundError: [Errno 2] No such file or directory: 'notes.txt'
   ok: not found: alice's file is in a different sandbox
10. bob (python): call out to the internet
   cut off after 5.2s
   ok: no answer from the internet: the sandbox has no network access
11. alice (python): read her own file back
   exit 0 in 0.5s
   stdout: alice's API token: tok_dummy_123
   ok: alice's file survived the infinite loop, the memory hog and the fork bomb
12. Waiting for the idle TTL to delete both sandboxes...
   [runner] bob: idle for 20s; deleting their sandbox
   [runner] deleted code-runner-ec8086d2
   [runner] alice: idle for 20s; deleting their sandbox
   [runner] deleted code-runner-a04f6fb7
13. 2 of 2 sandboxes are gone.
...
10 of 10 checks passed; 2 of 2 sandboxes deleted.
```

The token in step 2 is a dummy value the demo writes itself.

## Use it as a server

```bash
npm start                      # http://127.0.0.1:8080/run
curl -s localhost:8080/run -d '{"user": "alice", "language": "python", "code": "print(sum(range(10)))"}'
{"stdout":"45\n","stderr":"","exit_code":0,"timed_out":false,"duration_ms":417}
```

`POST /run` takes `user` (1 to 64 letters, digits or `. _ @ -`), `language` (`python` or `node`) and `code` (up to 64,000 characters). It returns `stdout`, `stderr` (each clipped to 10,000 characters), `exit_code`, `timed_out`, `duration_ms`, and `sandbox_restarted` when the whole sandbox restarted during the run. A new user while every sandbox is taken gets `429`; a failure on the NeevCloud side gets `502`.

Options: `--port` (8080), `--max-sandboxes` (3), `--idle-ttl` in seconds (120), `--run-timeout` in seconds (5, at most 60). The server listens on `127.0.0.1` only and has no authentication of its own: put it behind your app, which decides who `user` is.

`Ctrl+C` or `SIGTERM` deletes every sandbox before the server exits.

## How it works

1. **One sandbox per user, created on first use.** `neev.sandboxes.create({ resources: { cpu: 1, memory_gb: 2 }, egress: { mode: "deny_all" } })` gives each user an isolated Linux machine with no network access at all. Later requests from the same user reuse it, so their files persist between runs; no other user can see them, because they are on a different machine. The sandbox name is random (`code-runner-<hex>`) and never contains the user ID.
2. **Code runs as an unprivileged user.** Right after create, `sandbox.exec(["useradd", ..., "runner"])` adds a user with no login. Every run uses `setpriv` to drop to that user, so the code cannot signal or tamper with the sandbox's own processes.
3. **Every run has hard limits.** The code is sent on stdin to `python3 -` or `node -` through `sandbox.exec(argv, { stdin, timeoutMs })`, wrapped in `timeout -s KILL 5` and `prlimit --as=... --nproc=64`. The address-space cap applies to each process: 512 MB for Python, and 2 GB for Node, which reserves about 1.3 GB it never uses, so a Node process gets roughly 600 MB. A single memory hog therefore fails inside its own run. After the run, `pkill -KILL -u runner` kills anything the code left behind, including processes that escaped with `setsid`.
4. **Runs take turns, and a broken sandbox is replaced.** One user's runs execute one at a time. Because the memory cap is per process, code that forks several large processes can still exhaust the whole sandbox; NeevCloud then restarts it, the server reports `sandbox_restarted`, deletes it, and the user's next run gets a fresh sandbox. Other users are not affected: their code runs in other sandboxes.
5. **Sandboxes are capped and cleaned up.** At most 3 sandboxes exist at once. A sandbox idle for `--idle-ttl` seconds is deleted with `sandbox.delete()`, and so is every sandbox on shutdown.

The wall-clock limit is enforced in three places: `timeout` inside the sandbox (which keeps partial output), the sandbox's own exec deadline (`timeoutMs`, 5 seconds later), and a timer in the server (15 seconds after that) in case no reply arrives at all.

## What NeevCloud actually enforces

These are what the demo and test runs showed on NeevCloud, not just what the code asks for:

- **CPU and memory.** A sandbox created with `memory_gb: 2` reports 2048 MB total and no swap. With `cpu: 1`, two or four busy processes together got no more work done than one, so the sandbox gets one CPU's worth of time even though `nproc` reports 2.
- **Memory per process.** Python stopped with `MemoryError` at 448 MB and Node with `ERR_MEMORY_ALLOCATION_FAILED` at 640 MB; the sandbox stayed up and its files stayed in place. Without the cap, a memory hog takes down the whole sandbox instead: NeevCloud restarts it with an empty filesystem and records `lastCrash.reason: "OOMKilled"`, and the call that triggered it did not return for about two minutes. That is why the cap exists. Eight processes of 400 MB each in one run still do this, since the cap is per process: the server answered after 20 seconds with `timed_out: true, sandbox_restarted: "OOMKilled"`, deleted that sandbox, and the user's next run got a fresh one.
- **Processes.** With `--nproc=64`, `fork()` fails with `Resource temporarily unavailable`, and the sandbox stays responsive.
- **Network.** With `deny_all`, nothing gets out, and even name lookups get no answer, so a request does not fail fast: it hangs until the run's time limit kills it.

## Time and cost

The demo takes about 50 seconds: under 3 seconds to create each sandbox, the 5-second limits on the loop, fork bomb and network runs, and a 20-second idle wait at the end. It uses two sandboxes of 1 vCPU and 2 GB for under a minute each. No model tokens.

## Cleanup

Every sandbox is deleted when its user has been idle for the TTL, when the server or demo exits, fails or gets `Ctrl+C`, and if creating or setting it up fails part-way. Each one is also created with a platform idle timeout of 10 minutes set to delete, as a backstop if the process is killed outright; if that happens, also check the console for leftover `code-runner-` sandboxes.
