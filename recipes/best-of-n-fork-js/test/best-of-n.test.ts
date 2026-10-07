import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { REQUIRED_ENV, STRATEGIES, attempt, diff, loadFixture, missingEnv, passed, run, testCommand, testsRan, verify, type NeevLike } from "../best-of-n.ts";
import { FIXED, FakeSandboxes, connector, fakeModel, fakeSession, text, toolCall } from "./fakes.ts";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const FIXTURE = loadFixture(join(HERE, "..", "fixture"));
const TEMPS = STRATEGIES.map(([t]) => t);
const FIX = toolCall("fs_write", { path: "project/scheduler/intervals.py",
  content: FIXTURE["scheduler/intervals.py"].replace("merged[-1][1] = end", `merged[-1][1] = ${FIXED}`) });
const FINISH = toolCall("finish", { summary: "keep the larger end" }, "c2");
const STALL = Array.from({ length: 30 }, () => toolCall("fs_list", {}));
const SOLVE = () => Object.fromEntries(TEMPS.map((t) => [t, [FIX, FINISH]]));
const inProject = () => new Map(Object.entries(FIXTURE).map(([p, c]) => [`project/${p}`, c]));

// go runs the recipe against fakes; abort is the Ctrl+C a test can press.
async function go(model: any, opts: { fixture?: Record<string, string>; lines?: string[]; sandboxes?: FakeSandboxes; abort?: AbortController } = {}) {
  const sandboxes = opts.sandboxes ?? new FakeSandboxes();
  const lines = opts.lines ?? [];
  const code = await run({ sandboxes } as unknown as NeevLike, model, "m", connector(sandboxes), opts.fixture ?? FIXTURE,
    { log: (l) => lines.push(l), sleep: async () => {}, deadlineMs: 5000, signal: opts.abort?.signal });
  return { code, sandboxes, out: lines.join("\n") };
}

test("the fixture ships a package and its tests", () => {
  for (const p of ["scheduler/intervals.py", "scheduler/slots.py", "tests/test_intervals.py", "tests/test_slots.py"]) assert.ok(p in FIXTURE, p);
  assert.ok(FIXTURE["scheduler/intervals.py"].includes("merged[-1][1] = end"));
});

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"]);
});

test("the script exits 2 naming the missing variables before creating anything", () => {
  const env = { ...process.env };
  for (const name of REQUIRED_ENV) delete env[name];
  const r = spawnSync(process.execPath, ["--import", "tsx", "best-of-n.ts"], { cwd: join(HERE, ".."), env, encoding: "utf8" });
  assert.equal(r.status, 2);
  assert.match(r.stderr, /NEEV_API_KEY/);
});

test("the test command runs only the shipped test modules, in isolated mode", () => {
  const argv = testCommand(FIXTURE);
  assert.deepEqual(argv.slice(0, 3), ["python3", "-I", "-c"]);
  assert.ok(argv[3].includes("'tests.test_intervals', 'tests.test_slots'"));
  assert.ok(argv[3].includes("sys.path.append('.')")); // after the stdlib, so the project cannot shadow unittest
});

test("a pass needs exit 0, every test run and none skipped", () => {
  const cases: [number, string, boolean][] = [
    [0, "Ran 12 tests in 0.1s\n\nOK\n", true],
    [1, "Ran 12 tests in 0.1s\n\nFAILED (failures=3)\n", false],
    [0, "Ran 3 tests in 0.1s\n\nOK\n", false], // tests went missing
    [0, "", false], // exited 0 without running anything
    [0, "Ran 12 tests in 0.1s\n\nOK (skipped=2)\n", false],
  ];
  for (const [code, output, want] of cases) assert.equal(passed(code, output, 12), want, output);
  assert.equal(testsRan("Ran 1 test in 0.0s\n"), 1);
});

test("verify puts the original tests back before running them", async () => {
  const session = fakeSession(inProject());
  session.files.set("project/tests/test_slots.py", "SKIP_ALL");
  const [ok] = await verify(session, FIXTURE, 12);
  assert.equal(ok, false);
  assert.equal(session.files.get("project/tests/test_slots.py"), FIXTURE["tests/test_slots.py"]);
  assert.deepEqual(session.calls.at(-1)![1].args.slice(0, 2), ["-I", "-c"]);
});

test("verify reports a refused test run as a failure", async () => {
  const session = fakeSession(inProject());
  const real = session.callTool;
  session.callTool = async (params, schema, options) => params.name === "exec"
    ? { isError: true, content: [{ type: "text", text: "the sandbox refused this call: deadline_exceeded" }] }
    : real(params, schema, options);
  assert.deepEqual(await verify(session, FIXTURE, 12), [false, "the sandbox refused this call: deadline_exceeded"]);
});

test("the diff shows only the changed package files", async () => {
  const session = fakeSession(inProject());
  session.files.set("project/scheduler/intervals.py", FIXTURE["scheduler/intervals.py"].replace("merged[-1][1] = end", `merged[-1][1] = ${FIXED}`));
  const d = await diff(session, FIXTURE);
  assert.ok(d.startsWith("--- a/scheduler/intervals.py\n+++ b/scheduler/intervals.py\n"), d);
  assert.ok(d.includes(`+            merged[-1][1] = ${FIXED}`));
  assert.ok(!d.includes("slots.py"));
});

test("happy path: uploads once, forks three times, picks the winner and deletes everything", async () => {
  const model = fakeModel({ [TEMPS[0]]: STALL, [TEMPS[1]]: [FIX, FINISH], [TEMPS[2]]: STALL }, { [TEMPS[0]]: 50, [TEMPS[2]]: 50 });
  const { code, sandboxes, out } = await go(model);
  const [base, ...forks] = sandboxes.made;
  assert.equal(code, 0);
  assert.deepEqual(sandboxes.created, [{ name: base.name, egress: { mode: "deny_all" } }]);
  assert.match(base.name, /^best-of-n-js-[0-9a-f]{8}$/);
  assert.deepEqual(forks.map((f) => f.name), [1, 2, 3].map((i) => `${base.name}-${i}`));
  assert.ok(forks.every((f) => f.ready));
  assert.deepEqual(sandboxes.execs, [[base.name, testCommand(FIXTURE), "project"]]); // confirmed failing once, on the base
  assert.ok(sandboxes.made.every((s) => s.deleted));
  assert.ok(out.includes(`Winner: ${forks[1].name}`) && out.includes("+            merged[-1][1] = max"), out);
  assert.match(out, /cancelled/);
});

test("losers are cancelled as soon as one fork passes, and their sessions closed", async () => {
  const sandboxes = new FakeSandboxes();
  const model = fakeModel({ [TEMPS[0]]: [text("thinking")], [TEMPS[1]]: [FIX, FINISH], [TEMPS[2]]: [text("thinking")] }, { [TEMPS[0]]: 30_000, [TEMPS[2]]: 30_000 });
  const t = Date.now();
  const connect = connector(sandboxes);
  const code = await run({ sandboxes } as unknown as NeevLike, model, "m", connect, FIXTURE, { log: () => {}, sleep: async () => {} });
  assert.equal(code, 0);
  assert.ok(Date.now() - t < 5000);
  assert.ok(sandboxes.made.every((s) => s.deleted));
  assert.ok(connect.sessions.every((s) => s.closed));
});

test("forks retry while the base is still being snapshotted", async () => {
  const sandboxes = new FakeSandboxes();
  sandboxes.conflicts = 2;
  const { code } = await go(fakeModel(SOLVE()), { sandboxes });
  assert.equal(code, 0);
  assert.equal(sandboxes.made.length, 4);
});

test("no fork passing exits 1 and deletes everything", async () => {
  const lines: string[] = [];
  const { code, sandboxes } = await go(fakeModel(Object.fromEntries(TEMPS.map((t) => [t, STALL]))), { lines });
  assert.equal(code, 1);
  assert.ok(sandboxes.made.every((s) => s.deleted));
  assert.equal(lines.filter((l) => l.includes("temperature") && l.includes("failed after")).length, 3);
  assert.ok(lines.some((l) => l.includes("No fork")));
});

test("one crashing agent does not stop the others", async () => {
  const model = fakeModel({ [TEMPS[0]]: [new Error("500 Internal Server Error")], [TEMPS[1]]: [FIX, FINISH], [TEMPS[2]]: STALL }, { [TEMPS[1]]: 50, [TEMPS[2]]: 50 });
  const { code, out } = await go(model);
  assert.equal(code, 0);
  assert.match(out, /error after \d+s: Error: 500 Internal Server Error/);
});

test("a base where the tests already pass is refused before forking", async () => {
  const { code, sandboxes, out } = await go(fakeModel({}), { fixture: { ...FIXTURE, "scheduler/intervals.py": FIXED } });
  assert.equal(code, 1);
  assert.equal(sandboxes.made.length, 1);
  assert.ok(sandboxes.made[0].deleted);
  assert.match(out, /expected the tests to fail/);
});

test("Ctrl+C while the forks start deletes the base and the forks made so far", async () => {
  const sandboxes = new FakeSandboxes();
  const abort = new AbortController();
  const create = sandboxes.create.bind(sandboxes);
  sandboxes.create = async (params) => { const sb = await create(params); sandboxes.interruptReady.add(`${sb.name}-2`); return sb; };
  sandboxes.onInterrupt = () => abort.abort();
  const { code, out } = await go(fakeModel({}), { sandboxes, abort });
  assert.equal(code, 130);
  assert.match(out, /Interrupted\./);
  assert.equal(sandboxes.made.length, 4);
  assert.ok(sandboxes.made.every((s) => s.deleted));
});

test("Ctrl+C during the race stops every agent and deletes everything", async () => {
  const abort = new AbortController();
  const model = fakeModel(Object.fromEntries(TEMPS.map((t) => [t, [text("thinking")]])), Object.fromEntries(TEMPS.map((t) => [t, 30_000])));
  const t = Date.now();
  setTimeout(() => abort.abort(), 100);
  const { code, sandboxes } = await go(model, { abort });
  assert.equal(code, 130);
  assert.ok(Date.now() - t < 5000);
  assert.ok(sandboxes.made.every((s) => s.deleted));
});

test("a failed delete still deletes the rest", async () => {
  const sandboxes = new FakeSandboxes();
  const create = sandboxes.create.bind(sandboxes);
  sandboxes.create = async (params) => { const sb = await create(params); sb.delete = async () => { throw new Error("HTTP 503"); }; return sb; };
  const lines: string[] = [];
  const { code } = await go(fakeModel(SOLVE()), { sandboxes, lines });
  assert.equal(code, 0);
  assert.ok(sandboxes.made.slice(1).every((s) => s.deleted));
  assert.ok(lines.some((l) => l.includes("Could not delete") && l.includes("HTTP 503")));
});

test("runs started together get different names", async () => {
  const a = await go(fakeModel(SOLVE()));
  const b = await go(fakeModel(SOLVE()));
  assert.notEqual(a.sandboxes.made[0].name, b.sandboxes.made[0].name);
});

test("fork gives up after bounded retries and deletes the base", async () => {
  const sandboxes = new FakeSandboxes();
  sandboxes.conflicts = 100;
  const { code, out } = await go(fakeModel({}), { sandboxes });
  assert.equal(code, 1);
  assert.equal(sandboxes.made.length, 1);
  assert.ok(sandboxes.made[0].deleted);
  assert.match(out, /^Failed: ConflictError/m);
});

test("a fork whose reply was lost is found by name and deleted", async () => {
  const sandboxes = new FakeSandboxes();
  const create = sandboxes.create.bind(sandboxes);
  sandboxes.create = async (params) => { sandboxes.loseReplyFor.add(`${params.name}-2`); return create(params); };
  const { code } = await go(fakeModel({}), { sandboxes });
  assert.equal(code, 1);
  assert.deepEqual(sandboxes.made.map((s) => s.name.slice(-2)), [sandboxes.made[0].name.slice(-2), "-1", "-2"]);
  assert.ok(sandboxes.made.every((s) => s.deleted));
});

test("a hung tool call cannot outlast the agent's budget", async () => {
  const sandboxes = new FakeSandboxes();
  await sandboxes.create({ name: "fork-1" });
  const connect = async () => { const s = fakeSession(new Map(sandboxes.made[0].fs)); s.hangOn.add("exec"); return s; };
  const model = fakeModel({ [TEMPS[0]]: [toolCall("exec", { program: "python3", args: ["-m", "unittest"] })] });
  const t = Date.now();
  const a = await attempt(1, "fork-1", TEMPS[0], "hint", connect, model, "m", FIXTURE, 12, () => {}, { deadlineMs: 300 });
  assert.deepEqual([a.outcome, a.detail], ["failed", "time limit of 0.3s reached"]);
  assert.ok(Date.now() - t < 5000);
});

// project writes the fixture to a temporary folder, optionally with the fix applied.
function project(fixed: boolean) {
  const dir = mkdtempSync(join(tmpdir(), "best-of-n-"));
  for (const [path, content] of Object.entries(FIXTURE)) {
    mkdirSync(dirname(join(dir, path)), { recursive: true });
    writeFileSync(join(dir, path), fixed && path === "scheduler/intervals.py" ? content.replace("merged[-1][1] = end", `merged[-1][1] = ${FIXED}`) : content);
  }
  return dir;
}

const python = (argv: string[], cwd: string) => {
  const r = spawnSync(argv[0], argv.slice(1), { cwd, encoding: "utf8", timeout: 60_000 });
  return { code: r.status ?? -1, output: (r.stdout ?? "") + (r.stderr ?? "") };
};

test("the real test command fails on the shipped bug and passes once fixed", () => {
  for (const fixed of [false, true]) {
    const { code, output } = python(testCommand(FIXTURE), project(fixed));
    assert.equal(testsRan(output), 12, output);
    assert.equal(passed(code, output, 12), fixed, output);
    if (!fixed) assert.match(output, /FAILED \(failures=3\)/);
  }
});

test("a fake unittest in the project cannot fake a pass", () => {
  const dir = project(false);
  writeFileSync(join(dir, "unittest.py"), "import sys\nprint('Ran 12 tests in 0.0s\\n\\nOK', file=sys.stderr)\nsys.exit(0)\n");
  const { code, output } = python(testCommand(FIXTURE), dir);
  assert.equal(passed(code, output, 12), false);
});

test("nothing from the model or the sandbox reaches the terminal with control characters", async () => {
  const sneaky = toolCall("finish", { summary: "fixed\x1b[2J\x1b]0;owned\x07 ‮evil" }, "c2");
  const model = fakeModel({ [TEMPS[0]]: STALL, [TEMPS[1]]: [FIX, sneaky], [TEMPS[2]]: STALL }, { [TEMPS[0]]: 50, [TEMPS[2]]: 50 });
  const { code, out } = await go(model);
  assert.equal(code, 0);
  assert.ok(out.includes("fixed?[2J?]0;owned? ?evil"), out);
  assert.doesNotMatch(out, /[\x1b\x07‮]/);
});
