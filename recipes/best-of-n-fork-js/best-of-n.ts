// Best-of-N with fork: three agents race to fix the same bug, each in its own fork of one sandbox.
import { ConflictError } from "@neevcloud/sdk";
import { createTwoFilesPatch } from "diff";
import { randomBytes } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { join, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { AgentFailed, fixBug, type ModelLike, type SessionLike } from "./agent.ts";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY"] as const;
const MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp";
const MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1";
const DEFAULT_MODEL = "glm-4-7";
const FIXTURE_DIR = fileURLToPath(new URL("fixture", import.meta.url));
const PROJECT = "project"; // where the fixture lives in the sandbox, relative to the workspace root

// One agent per fork: same task and tools, a different temperature and approach.
export const STRATEGIES: readonly (readonly [number, string])[] = [
  [0.2, "Make the smallest change that fixes the bug. Read the failing tests, then the code they call."],
  [0.7, "Run the tests first and reason from each failing assertion back to the line that causes it."],
  [1.0, "Reproduce the bug with a short `python3 -c` command before editing, then fix its root cause."],
];

// Attempt is how one fork's agent did: outcome is passed, failed, error or cancelled.
export interface Attempt { fork: string; temperature: number; outcome: "passed" | "failed" | "error" | "cancelled"; seconds: number; detail: string; diff: string }

// SandboxLike is the part of an SDK sandbox handle this script uses.
interface SandboxLike {
  name: string;
  waitUntilReady(options?: { timeoutMs?: number }): Promise<unknown>;
  files: { write(path: string, content: string): Promise<unknown> };
  exec(command: string[], options?: { cwd?: string; signal?: AbortSignal }): Promise<{ exitCode: number; stdout: string; stderr: string }>;
  fork(name: string): Promise<SandboxLike>;
  delete(): Promise<unknown>;
}

export interface NeevLike {
  sandboxes: { create(params: Record<string, unknown>): Promise<SandboxLike>; list(params: { name?: string; limit?: number }): Promise<{ items: SandboxLike[] }> };
}

// Connect opens an MCP session bound to one sandbox.
export type Connect = (sandboxName: string, signal?: AbortSignal) => Promise<SessionLike & { close(): Promise<void> }>;

// CONTROL matches characters that can drive a terminal or reorder text: C0/C1 controls (not tab or newline) and bidi marks.
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]/g;

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// loadFixture reads the buggy package and its tests as {relative path: text}.
export function loadFixture(root: string): Record<string, string> {
  const paths = (readdirSync(root, { recursive: true }) as string[])
    .map((p) => p.split(sep).join("/"))
    .filter((p) => p.endsWith(".py") && !p.split("/").includes("__pycache__"))
    .sort();
  return Object.fromEntries(paths.map((p) => [p, readFileSync(join(root, p), "utf8")]));
}

// testCommand is the script's own test run: only the shipped test modules, with the project unable to shadow the stdlib.
// -I keeps the project directory and PYTHON* variables off sys.path; '.' is appended after the stdlib, so a
// file in the project such as unittest.py cannot stand in for the stdlib's test runner.
export function testCommand(fixture: Record<string, string>): string[] {
  const modules = Object.keys(fixture).filter((p) => p.startsWith("tests/test_")).map((p) => p.slice(0, -3).replaceAll("/", ".")).sort();
  const list = `[${modules.map((m) => `'${m}'`).join(", ")}]`;
  return ["python3", "-I", "-c", `import sys, unittest; sys.path.append('.'); unittest.main(module=None, argv=['unittest', *${list}])`];
}

// testsRan reads the test count from unittest's "Ran N tests" line.
export function testsRan(output: string): number | null {
  const found = /^Ran (\d+) tests? in/m.exec(output);
  return found ? Number(found[1]) : null;
}

// passed is true only for a clean exit that ran every expected test and reported a bare OK (no skips).
export function passed(exitCode: number, output: string, expected: number): boolean {
  return exitCode === 0 && testsRan(output) === expected && /^OK$/m.test(output);
}

// text joins an MCP result's text blocks.
const text = (r: { content?: unknown }) => (Array.isArray(r.content) ? r.content : []).map((b: any) => b.text ?? "").join("\n");

// verify puts the original tests back in the fork, runs them, and judges the result itself.
export async function verify(session: SessionLike, fixture: Record<string, string>, expected: number, signal?: AbortSignal): Promise<[boolean, string]> {
  for (const [path, content] of Object.entries(fixture)) {
    if (!path.startsWith("tests/")) continue;
    const r = await session.callTool({ name: "fs_write", arguments: { path: `${PROJECT}/${path}`, content } }, undefined, { signal });
    if (r.isError) return [false, `could not restore ${path}`];
  }
  const [program, ...args] = testCommand(fixture);
  const r = await session.callTool({ name: "exec", arguments: { program, args, cwd: PROJECT } }, undefined, { signal });
  if (r.isError) return [false, text(r)]; // e.g. the run outlived the sandbox's per-call time limit
  const out = (r.structuredContent ?? {}) as { exit_code?: number; stdout?: string; stderr?: string };
  const output = `${out.stdout ?? ""}${out.stderr ?? ""}`;
  return [passed(out.exit_code ?? -1, output, expected), output.slice(-3000)];
}

// diff is a unified diff of every package file in the fork against the fixture shipped with this recipe.
export async function diff(session: SessionLike, fixture: Record<string, string>, signal?: AbortSignal): Promise<string> {
  const chunks: string[] = [];
  for (const [path, original] of Object.entries(fixture)) {
    if (path.startsWith("tests/")) continue;
    const r = await session.callTool({ name: "fs_read", arguments: { path: `${PROJECT}/${path}` } }, undefined, { signal });
    const now = r.isError ? "" : String((r.structuredContent as { content?: string } | undefined)?.content ?? "");
    if (now === original) continue;
    const patch = createTwoFilesPatch(`a/${path}`, `b/${path}`, original, now, undefined, undefined, { context: 3 }).split("\n");
    // Keep the git-style part: from the --- line on, without the trailing tab the library puts after each name.
    chunks.push(patch.slice(patch.findIndex((l) => l.startsWith("--- "))).map((l) => /^(---|\+\+\+) /.test(l) ? l.replace(/\t$/, "") : l).join("\n"));
  }
  return chunks.join("");
}

// attempt runs one agent over an MCP session bound to its fork; any failure becomes this attempt's outcome.
// A cancel (another fork won, or Ctrl+C) passes through for the caller to handle.
export async function attempt(
  index: number, fork: string, temperature: number, hint: string, connect: Connect, modelClient: ModelLike, model: string,
  fixture: Record<string, string>, expected: number, log: (s: string) => void,
  opts: { deadlineMs?: number; maxSteps?: number; signal?: AbortSignal } = {},
): Promise<Attempt> {
  const { signal } = opts;
  const start = performance.now();
  const result = (outcome: Attempt["outcome"], detail: string, changes = ""): Attempt =>
    ({ fork, temperature, outcome, seconds: (performance.now() - start) / 1000, detail, diff: changes });
  let session: Awaited<ReturnType<Connect>> | undefined;
  try {
    session = await connect(fork, signal);
    const s = session;
    const summary = await fixBug(s, modelClient, model, temperature, hint, (bounded) => verify(s, fixture, expected, bounded),
      { ...opts, log: (line) => log(`   [${index}] ${line}`) });
    let changes: string;
    try {
      changes = await diff(s, fixture, signal);
    } catch (e) { // the fix is already verified; a failed read must not cost the win
      if (signal?.aborted) throw e;
      changes = `(could not read the changed files: ${(e as Error).message})`;
    }
    return result("passed", summary, changes);
  } catch (e) {
    if (signal?.aborted) throw e;
    if (e instanceof AgentFailed) return result("failed", e.message);
    return result("error", `${(e as Error).name}: ${(e as Error).message}`);
  } finally {
    await session?.close().catch(() => {});
  }
}

// race starts one agent per fork and returns as soon as one passes, cancelling the rest.
export async function race(
  forks: string[], connect: Connect, modelClient: ModelLike, model: string, fixture: Record<string, string>, expected: number,
  log: (s: string) => void, opts: { deadlineMs?: number; maxSteps?: number; signal?: AbortSignal } = {},
): Promise<{ results: Attempt[]; winner?: Attempt }> {
  const start = performance.now();
  const stop = new AbortController();
  const signal = opts.signal ? AbortSignal.any([opts.signal, stop.signal]) : stop.signal;
  const pending = new Map(forks.map((fork, i) => [i, attempt(i + 1, fork, STRATEGIES[i][0], STRATEGIES[i][1], connect, modelClient,
    model, fixture, expected, log, { ...opts, signal }).then((a) => ({ i, a }))]));
  const done = new Map<number, Attempt>();
  let winner: Attempt | undefined;
  let stopped = 0;
  try {
    while (pending.size && !winner) {
      const { i, a } = await Promise.race(pending.values());
      pending.delete(i);
      done.set(i, a);
      log(`   [${i + 1}] ${a.outcome} after ${a.seconds.toFixed(0)}s: ${a.detail}`);
      if (a.outcome === "passed") winner = a;
    }
  } finally {
    stopped = (performance.now() - start) / 1000;
    stop.abort();
    await Promise.allSettled(pending.values());
  }
  const results = forks.map((fork, i) => done.get(i) ?? { fork, temperature: STRATEGIES[i][0], outcome: "cancelled" as const, seconds: stopped, detail: "", diff: "" });
  return { results, winner };
}

// forkWithRetry forks base, retrying while the platform is still snapshotting it for the previous fork (409).
async function forkWithRetry(base: SandboxLike, name: string, sleep: (ms: number) => Promise<void>, tries = 20): Promise<SandboxLike> {
  for (let n = 1; ; n++) {
    try {
      return await base.fork(name);
    } catch (e) {
      if (!(e instanceof ConflictError) || n === tries) throw e;
      await sleep(500);
    }
  }
}

// lookup finds the sandbox with exactly this name, if one exists, so it can be deleted too.
async function lookup(neev: NeevLike, name: string, log: (s: string) => void): Promise<SandboxLike[]> {
  try {
    return (await neev.sandboxes.list({ name, limit: 100 })).items.filter((s) => s.name === name);
  } catch (e) {
    log(`   Could not check for a sandbox named ${name}: ${(e as Error).message}. Look for it in the console.`);
    return [];
  }
}

// run uploads the fixture to a base sandbox, races one agent per fork, and always deletes every sandbox.
export async function run(
  neev: NeevLike, modelClient: ModelLike, model: string, connect: Connect, fixture: Record<string, string>,
  opts: { log?: (s: string) => void; sleep?: (ms: number) => Promise<void>; deadlineMs?: number; maxSteps?: number; signal?: AbortSignal } = {},
): Promise<number> {
  const { signal, deadlineMs = 240_000, maxSteps = 20 } = opts;
  const rawLog = opts.log ?? console.log;
  // Every line passes here, so model or sandbox text cannot drive the terminal.
  const log = (line: string) => rawLog(line.replace(CONTROL, "?"));
  const sleep = opts.sleep ?? ((ms: number) => delay(ms, undefined, { signal }));
  // stopIfInterrupted ends the run between steps on Ctrl+C; the SDK's create and wait calls finish first.
  const stopIfInterrupted = () => signal?.throwIfAborted();
  const made: SandboxLike[] = []; // every sandbox this run created, deleted in finally
  // A create or fork whose reply was lost may still have made the sandbox; finally looks this name up.
  let asked = `best-of-n-js-${randomBytes(4).toString("hex")}`;
  try {
    log("1. Creating the base sandbox (no internet access)...");
    const base = await neev.sandboxes.create({ name: asked, egress: { mode: "deny_all" } });
    made.push(base);
    await base.waitUntilReady({ timeoutMs: 300_000 });
    stopIfInterrupted();
    log(`2. Uploading the buggy package (${Object.keys(fixture).length} files) to ${base.name} and running its tests...`);
    for (const [path, content] of Object.entries(fixture)) await base.files.write(`${PROJECT}/${path}`, content);
    stopIfInterrupted();
    const r = await base.exec(testCommand(fixture), { cwd: PROJECT, signal });
    const output = r.stdout + r.stderr;
    const expected = testsRan(output);
    if (!expected || passed(r.exitCode, output, expected)) {
      log(`The base sandbox did not show the bug: expected the tests to fail, got: ${output.trim().slice(-300)}`);
      return 1;
    }
    log(`   ${expected} tests ran: ${output.trim().split("\n").at(-1)}`);
    log(`3. Forking ${base.name} ${STRATEGIES.length} times...`);
    const start = performance.now();
    const forks: SandboxLike[] = [];
    for (let i = 1; i <= STRATEGIES.length; i++) {
      asked = `${base.name}-${i}`;
      forks.push(await forkWithRetry(base, asked, sleep));
      made.push(forks.at(-1)!);
      stopIfInterrupted();
    }
    for (const f of forks) {
      await f.waitUntilReady({ timeoutMs: 300_000 });
      stopIfInterrupted();
    }
    log(`   ${forks.length} forks ready in ${((performance.now() - start) / 1000).toFixed(1)}s, each with the same files`);
    log(`4. Racing ${forks.length} agents on ${model}, one per fork:`);
    forks.forEach((f, i) => log(`   [${i + 1}] ${f.name}  temperature ${STRATEGIES[i][0]}  ${STRATEGIES[i][1]}`));
    const { results, winner } = await race(forks.map((f) => f.name), connect, modelClient, model, fixture, expected, log, { deadlineMs, maxSteps, signal });
    log("5. Results:");
    for (const a of results) log(`   ${a.fork}  temperature ${a.temperature}  ${a.outcome} after ${a.seconds.toFixed(0)}s`);
    if (!winner) {
      log("No fork passed the tests.");
      return 1;
    }
    log(`Winner: ${winner.fork} (tests verified by the script). Its change:`);
    log(winner.diff.trimEnd());
    return 0;
  } catch (e) {
    if (signal?.aborted) { log("Interrupted."); return 130; }
    log(`Failed: ${(e as Error).name}: ${(e as Error).message}`); // e.g. a rejected key: one line instead of a stack trace
    return 1;
  } finally {
    if (!made.some((s) => s.name === asked)) made.push(...await lookup(neev, asked, log));
    let deleted = 0;
    for (const sb of made) {
      try {
        await sb.delete();
        deleted++;
      } catch (e) { // keep going: one failed delete must not leave the others
        log(`   Could not delete ${sb.name}: ${(e as Error).message}. Delete it from the console.`);
      }
    }
    if (deleted) log(`   Deleted ${deleted} sandboxes (the base and its forks).`);
  }
}

// mcpConnect returns a Connect for the sandbox MCP server, authenticated with the sandbox API key.
export function mcpConnect(apiKey: string): Connect {
  return async (sandboxName, signal) => {
    const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
    const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");
    const client = new Client({ name: "best-of-n-fork", version: "1.0.0" });
    const headers = { Authorization: `Bearer ${apiKey}`, "x-sandbox-name": sandboxName };
    await client.connect(new StreamableHTTPClientTransport(new URL(MCP_URL), { requestInit: { headers } }), { signal });
    return client as unknown as SessionLike & { close(): Promise<void> };
  };
}

// main parses arguments, checks the environment, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  try {
    parseArgs({ args: process.argv.slice(2), options: {} });
  } catch (e) { console.error((e as Error).message); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const { default: OpenAI } = await import("openai");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  const modelClient = new OpenAI({ baseURL: MODEL_BASE_URL, apiKey: process.env.NEEV_MODEL_API_KEY });
  return run(new Neev() as unknown as NeevLike, modelClient as unknown as ModelLike, process.env.MODEL ?? DEFAULT_MODEL,
    mcpConnect(process.env.NEEV_API_KEY!), loadFixture(FIXTURE_DIR), { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
