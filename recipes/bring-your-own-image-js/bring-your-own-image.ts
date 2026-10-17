// Bring your own image: run your toolchain image in a NeevCloud sandbox, then snapshot and fork it.
import { randomBytes } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID"] as const;
export const DEFAULT_IMAGE = "docker.io/library/node:22-bookworm";
const PROJECT_DIR = fileURLToPath(new URL("./project/", import.meta.url));
// Commands start in the image's working directory (/ for node), and the files API only reaches /workspace.
export const WORKDIR = "/workspace/cart";
const TEST_COMMAND = ["node", "--test", "--test-reporter=tap", "cart.test.js"];
export const BUG = "subtotal - discountPercent";
export const FIX = "subtotal * (1 - discountPercent / 100)";
const RESULT = /^(ok|not ok) \d+ - (.+)$/;
const DETAIL = /^ {2}(expected|actual): (.+)$/;

interface ExecResult { stdout: string; stderr: string; exitCode: number }
export interface SandboxLike {
  name: string;
  waitUntilReady(o?: { timeoutMs?: number }): Promise<unknown>;
  exec(command: string[], o?: { cwd?: string; timeoutMs?: number }): Promise<ExecResult>;
  files: { write(path: string, content: string): Promise<unknown>; readText(path: string): Promise<string> };
  snapshot(o: { name: string; waitUntilReady: boolean }): Promise<unknown>;
  fork(name: string): Promise<SandboxLike>;
  delete(): Promise<void>;
}
export interface NeevLike {
  sandboxes: {
    create(params: { name: string; image: string }): Promise<SandboxLike>;
    list(params: { name: string; limit: number }): Promise<{ items: SandboxLike[] }>;
  };
}

// RunFailed stops the run; its message says why.
class RunFailed extends Error {}

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// imageProblem says what is wrong with an image reference the platform would reject, or null if it looks usable.
export function imageProblem(image: string): string | null {
  if (!image.includes("/")) return `${image}: use a full reference with a registry or namespace, for example ${DEFAULT_IMAGE}`;
  if (!image.includes("@") && !image.slice(image.lastIndexOf("/") + 1).includes(":")) {
    return `${image}: add an explicit tag or digest, for example ${image}:<tag>`;
  }
  return null;
}

// loadProject reads the bundled project as {file name: text}.
export function loadProject(dir = PROJECT_DIR): Record<string, string> {
  return Object.fromEntries(readdirSync(dir).filter((f) => f.endsWith(".js")).sort().map((f) => [f, readFileSync(dir + f, "utf8")]));
}

// testResults reads each test from node's TAP output as [passed, name, why it failed].
export function testResults(tap: string): [boolean, string, string][] {
  const results: [boolean, string, Record<string, string>][] = [];
  for (const line of tap.split("\n")) {
    const result = RESULT.exec(line);
    const detail = DETAIL.exec(line);
    if (result) results.push([result[1] === "ok", result[2], {}]);
    else if (detail && results.length) results[results.length - 1][2][detail[1]] = detail[2];
  }
  return results.map(([ok, name, d]) => [ok, name, d.expected && d.actual ? `expected ${d.expected}, got ${d.actual}` : ""]);
}

// runTests runs the project's tests in the sandbox, prints one line per test, and says whether all passed.
async function runTests(sandbox: SandboxLike, log: (s: string) => void): Promise<boolean> {
  const result = await sandbox.exec(TEST_COMMAND, { cwd: WORKDIR, timeoutMs: 60_000 });
  const results = testResults(result.stdout);
  if (!results.length) throw new RunFailed(`the test run printed no results: ${(result.stdout + result.stderr).trim().slice(-300)}`);
  for (const [ok, name, why] of results) log(`   ${ok ? "pass" : "FAIL"}  ${name}${why ? ` (${why})` : ""}`);
  const failed = results.filter(([ok]) => !ok).length;
  log(`   ${results.length - failed} passed, ${failed} failed`);
  return result.exitCode === 0 && failed === 0;
}

// checkNode returns the image's Node.js version, or fails naming what the default flow needs.
async function checkNode(sandbox: SandboxLike, image: string): Promise<string> {
  const result = await sandbox.exec(["node", "--version"]);
  const found = /^v(\d+)\./.exec(result.stdout.trim());
  if (result.exitCode !== 0 || !found || Number(found[1]) < 20) {
    throw new RunFailed(`${image} has no Node.js 20 or newer on its PATH, which this demo needs for \`node --test\`. Try ${DEFAULT_IMAGE}.`);
  }
  return result.stdout.trim();
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

// run runs the tests in a sandbox from image, forks it, fixes the bug in the fork only, and deletes both.
export async function run(
  image: string, neev: NeevLike, project: Record<string, string>,
  opts: { log?: (s: string) => void; signal?: AbortSignal; clock?: () => number } = {},
): Promise<number> {
  const { log = console.log, signal, clock = () => performance.now() / 1000 } = opts;
  // Ctrl+C is checked at each step, so the finally below always deletes what was made.
  const step = (s: string) => { signal?.throwIfAborted(); log(s); };
  const made: SandboxLike[] = [];
  const name = `byoi-${randomBytes(4).toString("hex")}`;
  // A create or fork whose reply was lost may still have made the sandbox; finally looks this name up.
  let asked = name;
  try {
    step(`1. Creating a sandbox from ${image}...`);
    let start = clock();
    const original = await neev.sandboxes.create({ name, image });
    made.push(original);
    await original.waitUntilReady({ timeoutMs: 300_000 });
    log(`   Ready in ${(clock() - start).toFixed(1)} s, with Node.js ${await checkNode(original, image)}`);
    step(`2. Uploading ${Object.keys(project).join(", ")} and running the tests...`);
    for (const [file, text] of Object.entries(project)) await original.files.write(`${WORKDIR}/${file}`, text);
    if (await runTests(original, log)) throw new RunFailed("the tests passed before the fix; the planted bug is missing from cart.js");
    step("3. Taking a snapshot of the failing state...");
    start = clock();
    // A fork while a snapshot is still capturing is refused (409), so wait for Ready.
    await original.snapshot({ name: "before-fix", waitUntilReady: true });
    log(`   Snapshot Ready in ${(clock() - start).toFixed(1)} s`);
    step("4. Forking the sandbox...");
    start = clock();
    asked = `${name}-fork`;
    const fork = await original.fork(asked);
    made.push(fork);
    await fork.waitUntilReady({ timeoutMs: 300_000 });
    log(`   Fork ${fork.name} Ready in ${(clock() - start).toFixed(1)} s`);
    step("5. Fixing the bug in the fork only, then running the tests there...");
    const source = await fork.files.readText(`${WORKDIR}/cart.js`);
    await fork.files.write(`${WORKDIR}/cart.js`, source.replace(BUG, FIX));
    const fixed = await runTests(fork, log);
    step(`6. Running the tests again in the original, ${original.name}...`);
    const stillBroken = !(await runTests(original, log));
    if (!(fixed && stillBroken)) throw new RunFailed("expected the fork to pass and the original to still fail");
    log("   Two independent copies of one image: the fork is fixed, the original still has the bug.");
    return 0;
  } catch (e) {
    if (signal?.aborted) return 130;
    // e.g. an image the platform cannot pull: one line instead of a stack trace.
    const message = (e as Error).message.split("\n")[0];
    log(e instanceof RunFailed ? `Failed: ${message}` : `Failed: ${(e as Error).name}: ${message}`);
    return 1;
  } finally {
    if (!made.some((s) => s.name === asked)) made.push(...await lookup(neev, asked, log));
    for (const sandbox of made.reverse()) {
      try { await sandbox.delete(); log(`   Deleted ${sandbox.name}.`); } catch (e) {
        log(`   Could not delete ${sandbox.name}: ${(e as Error).message}. Delete it from the console.`);
      }
    }
  }
}

// main parses arguments, checks the environment and the image reference, and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  let image: string;
  try {
    image = parseArgs({ args: process.argv.slice(2), options: { image: { type: "string", default: DEFAULT_IMAGE } } }).values.image!;
  } catch (e) { console.error((e as Error).message); return 2; }
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const problem = imageProblem(image);
  if (problem) { console.error(problem); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  return run(image, new Neev() as unknown as NeevLike, loadProject(), { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
