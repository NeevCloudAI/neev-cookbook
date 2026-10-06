import assert from "node:assert/strict";
import { test } from "node:test";
import { CapacityError, MAX_OUTPUT, MAX_PROCESSES, MEMORY_LIMIT, RUN_USER, Runner, runCommand, SETUP } from "../runner.ts";
import { deadline, fakeNeev, gate, reply, tick } from "./fakes.ts";

const quiet = { log: () => {} };

test("creates one locked-down sandbox per user and reuses it", async () => {
  const neev = fakeNeev(async () => reply("hi\n"));
  const runner = new Runner(neev, quiet);
  await runner.run("alice", "python", "print('hi')");
  await runner.run("alice", "python", "print('hi')");
  await runner.run("bob", "node", "console.log('hi')");
  assert.equal(neev.all.length, 2);
  const p = neev.all[0].params;
  assert.deepEqual(p.egress, { mode: "deny_all" });
  assert.deepEqual(p.resources, { cpu: 1, memory_gb: 2 });
  assert.equal(p.lifecycle.on_idle, "delete");
  assert.ok(p.name.startsWith("code-runner-") && !p.name.includes("alice"));
  assert.notEqual(neev.all[0].name, neev.all[1].name);
  assert.equal(neev.all[0].execs.length, 2);
  await runner.close();
});

test("the code goes in on stdin, under a memory limit and a kill timer", async () => {
  const neev = fakeNeev(async () => reply("3\n"));
  const runner = new Runner(neev, { ...quiet, runTimeoutMs: 4000 });
  const r = await runner.run("alice", "python", "print(1+2)");
  assert.deepEqual(r, { stdout: "3\n", stderr: "", exitCode: 0, timedOut: false, durationMs: r.durationMs });
  const { cmd, stdin, timeoutMs } = neev.all[0].execs[0];
  assert.deepEqual(cmd, runCommand("python", 4000));
  assert.equal(stdin, "print(1+2)");
  assert.ok(timeoutMs! > 4000, "the sandbox's own exec deadline is a backstop behind the kill timer");
  await runner.close();
});

test("runCommand runs as an unprivileged user under memory, process and time limits, then kills what is left", () => {
  const py = runCommand("python", 5000);
  assert.equal(py[0], "sh");
  const script = py[2];
  assert.ok(script.includes(`prlimit --as=${MEMORY_LIMIT.python} --nproc=${MAX_PROCESSES}`), script);
  assert.ok(script.includes(`setpriv --reuid=${RUN_USER} --regid=${RUN_USER} --clear-groups`), script);
  assert.ok(script.includes("timeout -s KILL 5 "), script);
  assert.ok(script.trimEnd().endsWith(`pkill -KILL -u ${RUN_USER}; exit $code`), "leftover processes are killed before the exit code is returned");
  assert.ok(script.startsWith(`pkill -KILL -u ${RUN_USER};`), "anything left by an earlier cut-off run is killed first");
  assert.deepEqual(py.slice(4), ["python3", "-u", "-"]);
  const js = runCommand("node", 2500);
  assert.ok(js[2].includes(`--as=${MEMORY_LIMIT.node}`) && js[2].includes("timeout -s KILL 3 "));
  assert.deepEqual(js.slice(4), ["node", "-"]);
});

test("each new sandbox gets the unprivileged user before any code runs", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, quiet);
  await runner.run("alice", "python", "x");
  assert.deepEqual(neev.all[0].setup, [SETUP]);
  await runner.close();
});

test("a sandbox whose setup fails is deleted and the run fails", async () => {
  const neev = fakeNeev();
  neev.setupReply = reply("", 1, "useradd: cannot lock /etc/passwd");
  const runner = new Runner(neev, quiet);
  await assert.rejects(runner.run("alice", "python", "x"), /cannot lock/);
  assert.ok(neev.all[0].deleted);
  assert.equal(runner.live, 0);
});

test("a run killed by the timer is reported as timed out with its partial output", async () => {
  const neev = fakeNeev(async () => { await tick(30); return reply("started\n", 124); });
  const runner = new Runner(neev, { ...quiet, runTimeoutMs: 20 });
  const r = await runner.run("alice", "python", "while True: pass");
  assert.equal(r.timedOut, true);
  assert.equal(r.stdout, "started\n");
  await runner.close();
});

test("a kill reported as 137 (SIGKILL) also counts as a timeout", async () => {
  const runner = new Runner(fakeNeev(async () => { await tick(30); return reply("", 137); }), { ...quiet, runTimeoutMs: 20 });
  assert.equal((await runner.run("alice", "python", "while True: pass")).timedOut, true);
  await runner.close();
});

test("after the sandbox restarts, it is deleted and the user's next run gets a fresh one", async () => {
  let hang = true;
  const neev = fakeNeev(() => (hang ? new Promise(() => {}) : Promise.resolve(reply("ok"))));
  const runner = new Runner(neev, { ...quiet, runTimeoutMs: 10, graceMs: 10 });
  const pending = runner.run("alice", "python", "hog()");
  await tick(5);
  neev.all[0].lastCrash = { reason: "OOMKilled", at: new Date().toISOString(), storage_reset: true };
  assert.equal((await pending).sandboxRestarted, "OOMKilled");
  assert.ok(neev.all[0].deleted);
  assert.equal(runner.live, 0);
  hang = false;
  assert.equal((await runner.run("alice", "python", "x")).stdout, "ok");
  assert.equal(neev.all.length, 2);
  assert.deepEqual(neev.all[1].setup.length, 1);
  await runner.close();
});

test("close waits for an idle delete that is already in flight", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, { ...quiet, idleTtlMs: 10 });
  await runner.run("alice", "python", "x");
  let finish!: () => void;
  neev.all[0].delete = () => new Promise<void>((r) => { finish = () => { neev.all[0].deleted = true; r(); }; });
  await tick(30);
  let closed = false;
  const closing = runner.close().then(() => { closed = true; });
  await tick(10);
  assert.equal(closed, false);
  finish();
  await closing;
  assert.ok(neev.all[0].deleted);
  assert.equal(runner.live, 0);
});

test("a fast exit with code 124 is not a timeout", async () => {
  const runner = new Runner(fakeNeev(async () => reply("", 124)), { ...quiet, runTimeoutMs: 5000 });
  assert.equal((await runner.run("alice", "python", "raise SystemExit(124)")).timedOut, false);
  await runner.close();
});

test("the sandbox's exec deadline also counts as a timeout", async () => {
  const runner = new Runner(fakeNeev(async () => { throw deadline(); }), quiet);
  const r = await runner.run("alice", "python", "x");
  assert.equal(r.timedOut, true);
  assert.equal(r.exitCode, null);
  await runner.close();
});

test("a run that never returns is cut off and a sandbox restart is reported", async () => {
  const neev = fakeNeev(() => new Promise(() => {}));
  const runner = new Runner(neev, { ...quiet, runTimeoutMs: 10, graceMs: 10 });
  const pending = runner.run("alice", "python", "hog()");
  await tick(5);
  neev.all[0].lastCrash = { reason: "OOMKilled", at: new Date().toISOString(), storage_reset: true };
  const r = await pending;
  assert.equal(r.timedOut, true);
  assert.equal(r.sandboxRestarted, "OOMKilled");
  await runner.close();
});

test("an old crash is not blamed on this run", async () => {
  const neev = fakeNeev(() => new Promise(() => {}));
  const runner = new Runner(neev, { ...quiet, runTimeoutMs: 10, graceMs: 10 });
  const pending = runner.run("alice", "python", "x");
  await tick(5);
  neev.all[0].lastCrash = { reason: "OOMKilled", at: "2020-01-01T00:00:00Z", storage_reset: true };
  assert.equal((await pending).sandboxRestarted, undefined);
  await runner.close();
});

test("other exec failures reach the caller", async () => {
  const runner = new Runner(fakeNeev(async () => { throw new Error("HTTP 502"); }), quiet);
  await assert.rejects(runner.run("alice", "python", "x"), /502/);
  await runner.close();
});

test("output is clipped so one run cannot flood the server", async () => {
  const runner = new Runner(fakeNeev(async () => reply("x".repeat(MAX_OUTPUT * 5), 0, "y".repeat(MAX_OUTPUT * 5))), quiet);
  const r = await runner.run("alice", "python", "x");
  assert.ok(r.stdout.length < MAX_OUTPUT + 100 && r.stdout.includes("truncated"));
  assert.ok(r.stderr.length < MAX_OUTPUT + 100);
  await runner.close();
});

test("a new user past the cap is refused without creating a sandbox", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, { ...quiet, maxSandboxes: 2 });
  await runner.run("alice", "python", "x");
  await runner.run("bob", "python", "x");
  await assert.rejects(runner.run("carol", "python", "x"), CapacityError);
  await runner.run("alice", "python", "x");
  assert.equal(neev.all.length, 2);
  await runner.close();
});

test("concurrent first requests from one user share one sandbox", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, quiet);
  await Promise.all([runner.run("alice", "python", "a"), runner.run("alice", "python", "b")]);
  assert.equal(neev.all.length, 1);
  await runner.close();
});

test("one user's runs take turns, so the memory limit holds per sandbox", async () => {
  const g = gate();
  const neev = fakeNeev(g.exec);
  const runner = new Runner(neev, quiet);
  const first = runner.run("alice", "python", "a");
  const second = runner.run("alice", "python", "b");
  await tick(10);
  assert.equal(neev.all[0].execs.length, 1);
  g.release();
  await Promise.all([first, second]);
  assert.equal(neev.all[0].execs.length, 2);
  await runner.close();
});

test("an idle sandbox is deleted after the TTL and frees its slot", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, { ...quiet, maxSandboxes: 1, idleTtlMs: 20 });
  await runner.run("alice", "python", "x");
  await tick(60);
  assert.ok(neev.all[0].deleted);
  assert.equal(runner.live, 0);
  await runner.run("bob", "python", "x");
  assert.equal(neev.all.length, 2);
  await runner.close();
});

test("the idle TTL restarts on every run and never fires mid-run", async () => {
  const g = gate();
  const neev = fakeNeev(g.exec);
  const runner = new Runner(neev, { ...quiet, idleTtlMs: 20 });
  const pending = runner.run("alice", "python", "x");
  await tick(60);
  assert.equal(neev.all[0].deleted, false);
  g.release();
  await pending;
  await tick(60);
  assert.ok(neev.all[0].deleted);
  await runner.close();
});

test("a failed create frees the slot and the next request tries again", async () => {
  const neev = fakeNeev();
  neev.failCreate = new Error("quota exceeded");
  const runner = new Runner(neev, { ...quiet, maxSandboxes: 1 });
  await assert.rejects(runner.run("alice", "python", "x"), /quota/);
  assert.equal(runner.live, 0);
  neev.failCreate = undefined;
  await runner.run("alice", "python", "x");
  assert.equal(neev.all.length, 1);
  await runner.close();
});

test("a sandbox that never gets ready is deleted", async () => {
  const neev = fakeNeev();
  neev.failReady = new Error("not ready");
  const runner = new Runner(neev, quiet);
  await assert.rejects(runner.run("alice", "python", "x"), /not ready/);
  assert.ok(neev.all[0].deleted);
  assert.equal(runner.live, 0);
});

test("close deletes every sandbox, including one still being created, and refuses new runs", async () => {
  const g = gate();
  const neev = fakeNeev(g.exec);
  const runner = new Runner(neev, quiet);
  const inflight = runner.run("alice", "python", "x").catch(() => {});
  const creating = runner.run("bob", "python", "x").catch(() => {});
  await runner.close();
  assert.ok(neev.all.every((s) => s.deleted));
  assert.equal(neev.all.length, 2);
  assert.equal(runner.live, 0);
  await assert.rejects(runner.run("carol", "python", "x"), /shutting down/);
  g.release();
  await Promise.all([inflight, creating]);
});

test("a failed delete is logged and does not stop the others", async () => {
  const neev = fakeNeev();
  const lines: string[] = [];
  const runner = new Runner(neev, { log: (s) => lines.push(s) });
  await runner.run("alice", "python", "x");
  await runner.run("bob", "python", "x");
  neev.all[0].delete = async () => { throw new Error("HTTP 500"); };
  await runner.close();
  assert.ok(neev.all[1].deleted);
  assert.ok(lines.some((l) => l.includes("could not delete") && l.includes("HTTP 500")));
});

test("names records every sandbox created", async () => {
  const neev = fakeNeev();
  const runner = new Runner(neev, quiet);
  await runner.run("alice", "python", "x");
  await runner.run("bob", "node", "x");
  assert.deepEqual(runner.names, neev.all.map((s) => s.name));
  await runner.close();
});
