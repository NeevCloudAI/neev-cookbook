# Run untrusted user code in your SaaS

Your product lets users run their own Python or JavaScript. This recipe is the backend for that: a small HTTP server that runs each user's code in that user's own NeevCloud sandbox, with no network, hard memory, process and time limits, and the sandbox deleted when the user goes idle.

<p align="center">
  <img src="../../assets/runs/untrusted-code-runner-js.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Node 20+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). No model key: nothing here calls a model.

```bash
npm install
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
npm run demo
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

The demo starts the server and sends it ordinary and hostile code as two users: an infinite loop, a memory hog, a fork bomb, a read of the other user's file and a call to the internet. It checks each one was contained, waits for the idle timeout to delete both sandboxes, and exits 0 only when every check passed.

## Use it as a server

```bash
npm start                      # http://127.0.0.1:8080/run
curl -s localhost:8080/run -d '{"user": "alice", "language": "python", "code": "print(sum(range(10)))"}'
{"stdout":"45\n","stderr":"","exit_code":0,"timed_out":false,"duration_ms":417}
```

`POST /run` takes `user`, `language` (`python` or `node`) and `code` (up to 64,000 characters), and returns `stdout`, `stderr`, `exit_code`, `timed_out` and `duration_ms`. A new user when every sandbox is taken gets `429`. Options: `--port` (8080), `--max-sandboxes` (3), `--idle-ttl` in seconds (120) and `--run-timeout` in seconds (5, at most 60).

The server listens on `127.0.0.1` only and has no authentication of its own: put it behind your app, which decides who `user` is.

## How it works

1. **One sandbox per user.** On a user's first run, the server creates a sandbox with 1 CPU, 2 GB of memory and no network access. Later runs from the same user reuse it, so their files persist; no other user can see them.
2. **An unprivileged user.** Code runs as a separate `runner` user, so it can't tamper with the sandbox's own processes.
3. **Hard limits.** Each run is killed at the time limit, each process is capped in memory, and the number of processes is capped. Anything left running afterwards is killed.
4. **One run at a time.** A user's runs execute in turn. If code still manages to crash the whole sandbox, the server replaces it and the user's next run gets a fresh one.
5. **Clean up.** A sandbox idle for `--idle-ttl` seconds is deleted, and so is every sandbox when the server stops.

## Use it in your product

- **Your code playground, grader or notebook:** call `POST /run` from your backend with the signed-in user's ID. Each user gets an isolated machine without you managing any infrastructure.
- **Your own limits:** `RESOURCES`, `MEMORY_LIMIT` and `MAX_PROCESSES` in `runner.ts` set the sandbox size and per-run caps; `--run-timeout` and `--idle-ttl` set the timing.
- **More languages:** in `runner.ts`, add the language to `Language`, `LANGUAGES` and `MEMORY_LIMIT`, and the command that runs it to `runCommand`. Any interpreter the sandbox has will do.

## What NeevCloud actually enforces

These are what the demo and test runs showed, not just what the code asks for:

- **CPU and memory.** A sandbox with `memory_gb: 2` has 2 GB and no swap. With `cpu: 1` it gets one CPU's worth of time.
- **Memory per process.** Python stopped with `MemoryError` at 448 MB and Node at 640 MB, and the sandbox stayed up. Without the cap, one memory hog takes the whole sandbox down and NeevCloud restarts it with an empty filesystem, which is why the cap exists.
- **Processes.** With 64 processes allowed, a fork bomb gets `Resource temporarily unavailable` and the sandbox stays responsive.
- **Network.** With `deny_all`, nothing gets out, and even name lookups get no answer, so a network call hangs until the run's time limit.

## Time and cost

The demo takes about 50 seconds and uses two sandboxes of 1 vCPU and 2 GB for under a minute each. No model tokens. Each sandbox is also created with a 10-minute platform idle timeout set to delete, as a backstop if the process is killed outright; if that happens, also check the console for leftover `code-runner-` sandboxes.
