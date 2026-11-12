import assert from "node:assert/strict";
import { test } from "node:test";
import { BUG, DEFAULT_IMAGE, FIX, WORKDIR, imageProblem, loadProject, missingEnv, run, testResults } from "../bring-your-own-image.ts";
import { fakeNeev, tap } from "./fakes.ts";

const PROJECT = loadProject();

async function go(f: ReturnType<typeof fakeNeev>, opts: { project?: Record<string, string>; signal?: AbortSignal } = {}) {
  const lines: string[] = [];
  const code = await run(DEFAULT_IMAGE, f.neev, opts.project ?? PROJECT, { log: (s) => lines.push(s), signal: opts.signal });
  return { code, lines };
}

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]);
});

test("imageProblem wants a slash and an explicit tag or digest", () => {
  for (const ok of [DEFAULT_IMAGE, `ghcr.io/acme/toolchain@sha256:${"a".repeat(64)}`, "registry.example.com:5000/team/node:22"]) {
    assert.equal(imageProblem(ok), null, ok);
  }
  for (const bad of ["node:22", "docker.io/library/node", "registry.example.com:5000/team/node"]) assert.ok(imageProblem(bad), bad);
});

test("the bundled project has the planted bug and its tests", () => {
  assert.deepEqual(Object.keys(PROJECT), ["cart.js", "cart.test.js"]);
  assert.ok(PROJECT["cart.js"].includes(BUG));
});

test("testResults reads each test and why it failed", () => {
  assert.deepEqual(testResults(tap(BUG)[1]), [
    [true, "adds up price times quantity", ""],
    [true, "an empty cart costs nothing", ""],
    [false, "takes 10% off", "expected 1800, got 1990"],
  ]);
});

test("happy path fixes only the fork and deletes both", async () => {
  const f = fakeNeev();
  const { code, lines } = await go(f);
  assert.equal(code, 0, lines.join("\n"));
  const [original, fork] = f.made;
  assert.deepEqual(f.created, [{ name: original.name, image: DEFAULT_IMAGE }]);
  assert.ok(original.name.startsWith("byoi-"));
  assert.equal(fork.name, `${original.name}-fork`);
  assert.deepEqual(original.snapshots, [{ name: "before-fix", waitUntilReady: true }]);
  assert.ok(original.store.get(`${WORKDIR}/cart.js`)!.includes(BUG));
  assert.ok(fork.store.get(`${WORKDIR}/cart.js`)!.includes(FIX));
  assert.ok([...original.execs, ...fork.execs].filter(([c]) => c[1] === "--test").every(([, cwd]) => cwd === WORKDIR));
  assert.ok(lines.includes("   FAIL  takes 10% off (expected 1800, got 1990)"));
  assert.ok(lines.filter((l) => l.includes("Ready in"))[2].startsWith(`   Fork ${fork.name} Ready in`));
  assert.ok(original.deleted && fork.deleted);
});

test("an image without node fails with what the demo needs", async () => {
  const f = fakeNeev({ node: null });
  const { code, lines } = await go(f);
  assert.equal(code, 1);
  assert.ok(f.made[0].deleted);
  assert.match(lines.at(-2)!, /Node\.js 20 or newer.*node:22-bookworm/);
});

test("an old node is refused", async () => {
  const f = fakeNeev({ node: "v18.12.0" });
  assert.equal((await go(f)).code, 1);
  assert.ok(f.made[0].deleted);
});

test("a rejected image is one line and leaves nothing to delete", async () => {
  const f = fakeNeev({ createError: Object.assign(new Error("HTTP 400 Unable to access the container image.\nThis registry..."), { name: "BadRequestError" }) });
  const { code, lines } = await go(f);
  assert.equal(code, 1);
  assert.equal(f.made.length, 0);
  assert.equal(lines.at(-1), "Failed: BadRequestError: HTTP 400 Unable to access the container image.");
});

test("a failed fork still deletes the original", async () => {
  const f = fakeNeev({ forkError: new Error("HTTP 409 A snapshot is already in progress") });
  assert.equal((await go(f)).code, 1);
  assert.deepEqual(f.made.map((s) => s.deleted), [true]);
});

test("a create whose reply was lost is found by name and deleted", async () => {
  const f = fakeNeev({ createError: new Error("fetch failed"), lostReply: true });
  assert.equal((await go(f)).code, 1);
  assert.equal(f.made.length, 1);
  assert.ok(f.made[0].deleted);
});

test("a fork whose reply was lost is found by name and deleted", async () => {
  const f = fakeNeev({ forkError: new Error("fetch failed"), lostReply: true });
  assert.equal((await go(f)).code, 1);
  const [original, fork] = f.made;
  assert.equal(fork.name, `${original.name}-fork`);
  assert.ok(original.deleted && fork.deleted);
});

test("a project without the bug is reported and deleted", async () => {
  const f = fakeNeev();
  const { code, lines } = await go(f, { project: { ...PROJECT, "cart.js": PROJECT["cart.js"].replace(BUG, FIX) } });
  assert.equal(code, 1);
  assert.equal(f.made.length, 1);
  assert.ok(f.made[0].deleted);
  assert.match(lines.at(-2)!, /planted bug is missing/);
});

test("Ctrl+C stops at the next step, deletes every sandbox and exits 130", async () => {
  const f = fakeNeev(); const ac = new AbortController();
  const create = f.neev.sandboxes.create;
  f.neev.sandboxes.create = async (p) => { const sb = await create(p); ac.abort(); return sb; };
  assert.equal((await go(f, { signal: ac.signal })).code, 130);
  assert.equal(f.made[0].snapshots.length, 0);
  assert.ok(f.made[0].deleted);
});

test("runs get different sandbox names", async () => {
  const f = fakeNeev();
  await go(f); await go(f);
  assert.notEqual((f.created[0] as { name: string }).name, (f.created[1] as { name: string }).name);
});
