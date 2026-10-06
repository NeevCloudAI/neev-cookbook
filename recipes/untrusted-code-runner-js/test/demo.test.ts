import assert from "node:assert/strict";
import { test } from "node:test";
import { runDemo } from "../demo.ts";
import { type ExecFn, fakeNeev, reply, tick } from "./fakes.ts";

const RUN_MS = 20;

// sandboxLike behaves like the real sandboxes did on prod, with a private file store per sandbox.
function sandboxLike(overrides: { leak?: boolean; hogKillsSandbox?: boolean } = {}): ExecFn {
  const files = new Map<string, Map<string, string>>();
  return async (_cmd, { stdin = "" }, sb) => {
    const own = files.get(sb.name) ?? new Map<string, string>();
    files.set(sb.name, own);
    if (stdin.includes("os.fork()")) { await tick(RUN_MS + 5); return reply("fork refused after 63 forks: Resource temporarily unavailable\n", 124); }
    if (stdin.includes("while True")) { await tick(RUN_MS + 5); return reply("spinning...\n", 124); }
    if (stdin.includes("bytearray")) {
      if (overrides.hogKillsSandbox) return new Promise(() => {});
      return reply("64 MB\n", 1, "MemoryError\n");
    }
    if (stdin.includes("Buffer.alloc")) return reply("64 MB\n", 1, "code: 'ERR_MEMORY_ALLOCATION_FAILED'\n");
    if (stdin.includes("urlopen")) return overrides.leak ? reply("LEAKED 1256\n") : (await tick(RUN_MS + 5), reply("", 124));
    if (stdin.includes("open(\"notes.txt\", \"w\")")) { own.set("notes.txt", "alice's API token: tok_dummy_123\n"); return reply("sum of 1..100 = 5050\n"); }
    if (stdin.includes("open(\"notes.txt\")")) {
      const v = own.get("notes.txt");
      return v ? reply(`files: ['notes.txt']\n${v}`) : reply("files: []\n", 1, "FileNotFoundError: [Errno 2] No such file or directory: 'notes.txt'\n");
    }
    if (stdin.includes("console.log")) return reply("bob runs Node v24.19.0\n");
    return reply("", 0);
  };
}

const opts = (lines: string[], extra = {}) => ({ log: (s: string) => lines.push(s), idleTtlMs: 200, runTimeoutMs: RUN_MS, graceMs: 30, ...extra });

test("every containment check passes and every sandbox is deleted: exit 0", async () => {
  const neev = fakeNeev(sandboxLike()); const lines: string[] = [];
  assert.equal(await runDemo(neev, opts(lines)), 0, lines.join("\n"));
  assert.equal(neev.all.length, 2, "carol was refused, so only alice and bob got sandboxes");
  assert.ok(neev.all.every((s) => s.deleted));
  assert.ok(lines.some((l) => l.includes("10 of 10 checks passed")), lines.join("\n"));
  assert.ok(lines.some((l) => l.includes("idle")));
  assert.ok(lines.some((l) => l.includes("2 of 2 sandboxes are gone")));
});

test("a network call that gets through fails the demo", async () => {
  const neev = fakeNeev(sandboxLike({ leak: true })); const lines: string[] = [];
  assert.equal(await runDemo(neev, opts(lines)), 1);
  assert.ok(lines.some((l) => l.includes("FAILED")));
  assert.ok(neev.all.every((s) => s.deleted));
});

test("a memory hog that takes the sandbox down fails the check and still cleans up", async () => {
  const neev = fakeNeev(sandboxLike({ hogKillsSandbox: true })); const lines: string[] = [];
  assert.equal(await runDemo(neev, opts(lines)), 1);
  assert.ok(neev.all.every((s) => s.deleted));
});

test("a sandbox that is still listed after shutdown fails the demo", async () => {
  const neev = fakeNeev(sandboxLike()); const lines: string[] = [];
  neev.sandboxes.list = async () => ({ items: [{}] });
  assert.equal(await runDemo(neev, opts(lines)), 1);
  assert.ok(lines.some((l) => l.includes("still exists")));
});

test("Ctrl+C part-way deletes every sandbox and exits 130", async () => {
  const ac = new AbortController(); const lines: string[] = [];
  const neev = fakeNeev(sandboxLike());
  const log = (s: string) => { lines.push(s); if (s.includes("an infinite loop")) ac.abort(); };
  assert.equal(await runDemo(neev, { ...opts(lines), log, signal: ac.signal }), 130);
  assert.ok(neev.all.length > 0 && neev.all.every((s) => s.deleted));
});

test("a platform failure is one line, cleans up, and exits 1", async () => {
  const neev = fakeNeev(sandboxLike()); const lines: string[] = [];
  neev.failCreate = new Error("HTTP 403 forbidden");
  assert.equal(await runDemo(neev, opts(lines)), 1);
  assert.ok(lines.some((l) => l.includes("HTTP 403")));
});
