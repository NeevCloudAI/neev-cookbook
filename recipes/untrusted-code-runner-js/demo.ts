// Demo: start the code runner, send it ordinary and hostile code as two users, and check each was contained.
import type { AddressInfo } from "node:net";
import { setTimeout as sleep } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { type NeevLike, Runner } from "./runner.ts";
import { createServer, missingEnv } from "./server.ts";

// DemoNeev also lists sandboxes by name, to confirm from the API that every one is gone.
export type DemoNeev = NeevLike & { sandboxes: { list(params: { name: string }): Promise<{ items: unknown[] }> } };

interface Reply { status: number; body: any }
interface Step { user: string; language: "python" | "node"; title: string; code: string; ok(r: Reply): boolean; expect: string }

const SECRET = "tok_dummy_123";
// ran describes a run's outcome in a few words.
const ran = (r: Reply) => r.status !== 200 ? `HTTP ${r.status}`
  : r.body.timed_out ? `cut off after ${(r.body.duration_ms / 1000).toFixed(1)}s` : `exit ${r.body.exit_code} in ${(r.body.duration_ms / 1000).toFixed(1)}s`;
// failedOnItsOwn reports a run that ended on its own (not cut off) with a failing exit code.
const failedOnItsOwn = (r: Reply) => r.status === 200 && r.body.exit_code !== 0 && !r.body.timed_out && !r.body.sandbox_restarted;

// The requests the demo sends, in order, with what each must show to count as contained.
export const STEPS: Step[] = [
  { user: "alice", language: "python", title: "ordinary code that saves a private file",
    code: `open("notes.txt", "w").write("alice's API token: ${SECRET}\\n")\nprint("sum of 1..100 =", sum(range(1, 101)))`,
    ok: (r) => r.status === 200 && r.body.exit_code === 0 && r.body.stdout.includes("5050"),
    expect: "ran normally in alice's own sandbox" },
  { user: "bob", language: "node", title: "ordinary Node code",
    code: `console.log("bob runs Node", process.version)`,
    ok: (r) => r.status === 200 && r.body.exit_code === 0,
    expect: "ran normally in bob's own sandbox" },
  { user: "carol", language: "python", title: "a third user while the cap is 2 sandboxes",
    code: `print("hello")`,
    ok: (r) => r.status === 429,
    expect: "refused with 429: no third sandbox is created" },
  { user: "alice", language: "python", title: "an infinite loop",
    code: `print("spinning...")\nwhile True:\n    pass`,
    ok: (r) => r.status === 200 && r.body.timed_out === true,
    expect: "killed at the per-run time limit" },
  { user: "alice", language: "python", title: "a memory hog (64 MB at a time, up to 4 GB)",
    code: `chunks = []\nfor i in range(64):\n    chunks.append(bytearray(64 * 1024 * 1024))\n    print((i + 1) * 64, "MB", flush=True)`,
    ok: (r) => failedOnItsOwn(r) && r.body.stderr.includes("MemoryError"),
    expect: "MemoryError at the per-process memory cap; the sandbox stayed up" },
  { user: "alice", language: "python", title: "a fork bomb (every process forks until refused)",
    code: `import os, time\nroot, forks = os.getpid(), 0\nwhile True:\n    try:\n        os.fork()\n        forks += 1\n    except OSError as e:\n        if os.getpid() == root:\n            print("fork refused after", forks, "forks:", e.strerror, flush=True)\n        break\ntime.sleep(60)`,
    ok: (r) => r.status === 200 && r.body.stdout.includes("fork refused") && r.body.timed_out && !r.body.sandbox_restarted,
    expect: "forks refused at the per-run process cap, and every process killed at the time limit" },
  { user: "bob", language: "node", title: "a memory hog (64 MB buffers, up to 4 GB)",
    code: `const chunks = [];\nfor (let i = 0; i < 64; i++) { chunks.push(Buffer.alloc(64 * 1024 * 1024, 1)); console.log((i + 1) * 64, "MB"); }`,
    ok: (r) => failedOnItsOwn(r),
    expect: "allocation failed at the per-process memory cap; the sandbox stayed up" },
  { user: "bob", language: "python", title: "try to read alice's private file",
    code: `import os\nprint("files:", os.listdir("."))\nprint(open("notes.txt").read())`,
    ok: (r) => r.status === 200 && r.body.exit_code !== 0 && !r.body.stdout.includes(SECRET) && r.body.stderr.includes("FileNotFoundError"),
    expect: "not found: alice's file is in a different sandbox" },
  { user: "bob", language: "python", title: "call out to the internet",
    code: `import urllib.request\nbody = urllib.request.urlopen("https://example.com", timeout=3).read()\nprint("LEAKED", len(body))`,
    ok: (r) => r.status === 200 && !r.body.stdout.includes("LEAKED") && (r.body.timed_out || r.body.exit_code !== 0),
    expect: "no answer from the internet: the sandbox has no network access" },
  { user: "alice", language: "python", title: "read her own file back",
    code: `print(open("notes.txt").read())`,
    ok: (r) => r.status === 200 && r.body.exit_code === 0 && r.body.stdout.includes(SECRET),
    expect: "alice's file survived the infinite loop, the memory hog and the fork bomb" },
];

// lastLine returns the last non-empty line of some output, shortened for the progress log.
const lastLine = (s: string) => (s.trim().split("\n").pop() ?? "").slice(0, 100);
// errorLine prefers the last line that names an error, since runtimes print a version or stack after it.
const errorLine = (s: string) => (s.split("\n").reverse().find((l) => /Error|ERR_/.test(l)) ?? lastLine(s)).trim().slice(0, 100);

// runDemo serves the runner on a free local port, sends STEPS, waits for the idle TTL, and confirms every sandbox is gone.
export async function runDemo(
  neev: DemoNeev,
  opts: { log?: (s: string) => void; signal?: AbortSignal; idleTtlMs?: number; runTimeoutMs?: number; graceMs?: number } = {},
): Promise<number> {
  const { log = console.log, signal, idleTtlMs = 20_000, runTimeoutMs = 5_000, graceMs } = opts;
  const runner = new Runner(neev, { maxSandboxes: 2, idleTtlMs, runTimeoutMs, graceMs, log });
  const server = createServer(runner);
  const recap: string[] = [];
  let passed = 0;
  let n = 1;
  try {
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/run`;
    log(`${n++}. Code runner on ${url}: at most 2 sandboxes, ${runTimeoutMs / 1000}s per run, no internet, idle sandboxes deleted after ${idleTtlMs / 1000}s`);
    for (const step of STEPS) {
      log(`${n++}. ${step.user} (${step.language}): ${step.title}`);
      const res = await fetch(url, { method: "POST", body: JSON.stringify({ user: step.user, language: step.language, code: step.code }), signal });
      const reply: Reply = { status: res.status, body: await res.json() };
      if (reply.status >= 500) throw new Error(reply.body.error);
      const b = reply.body;
      if (reply.status === 200) {
        log(`   ${ran(reply)}${b.sandbox_restarted ? `, sandbox restarted (${b.sandbox_restarted})` : ""}`);
        if (b.stdout.trim()) log(`   stdout: ${lastLine(b.stdout)}`);
        if (b.stderr.trim()) log(`   stderr: ${errorLine(b.stderr)}`);
      } else {
        log(`   HTTP ${reply.status}: ${b.error}`);
      }
      const ok = step.ok(reply);
      if (ok) passed++;
      log(ok ? `   ok: ${step.expect}` : `   FAILED: expected ${step.expect}`);
      recap.push(`   ${ok ? "ok" : "FAILED"}: ${step.user}, ${step.title}: ${ran(reply)}`);
    }
    log(`${n++}. Waiting for the idle TTL to delete both sandboxes...`);
    const deadline = Date.now() + idleTtlMs + 30_000;
    while (runner.live > 0 && Date.now() < deadline) await sleep(Math.min(500, idleTtlMs), undefined, { signal });
  } catch (err) {
    if (signal?.aborted) { log("Interrupted: deleting every sandbox..."); return 130; }
    log(`Failed: ${(err as Error).message}`);
    return 1;
  } finally {
    server.close();
    await runner.close();
  }
  // Ask the API, not the runner's own bookkeeping, whether each sandbox is really gone.
  let gone = 0;
  for (const name of runner.names) {
    if ((await neev.sandboxes.list({ name })).items.length === 0) gone++; else log(`   ${name} still exists`);
  }
  log(`${n}. ${gone} of ${runner.names.length} sandboxes are gone.`);
  log("Summary:");
  for (const line of recap) log(line);
  log(`${passed} of ${STEPS.length} checks passed; ${gone} of ${runner.names.length} sandboxes deleted.`);
  return passed === STEPS.length && gone === runner.names.length ? 0 : 1;
}

// main checks the environment and runs the demo with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const ac = new AbortController();
  // Keep the handlers for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  process.on("SIGTERM", () => ac.abort());
  return runDemo(new Neev() as unknown as DemoNeev, { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
