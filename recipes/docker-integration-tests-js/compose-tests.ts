// Disposable integration tests: bring up an app with real Postgres and Redis in a sandbox, test it, tear it all down.
import { randomBytes } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath, pathToFileURL } from "node:url";

export const REQUIRED_ENV = ["NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID"] as const;
const TEMPLATE = "sb-ubuntu-26-04-docker";
export const PROJECT_DIR = fileURLToPath(new URL("./sample-app", import.meta.url));
export const REMOTE_DIR = "/workspace/sample-app";
// The daemon starts at boot, so give it a moment before the first docker call.
const VERSIONS =
  "for i in $(seq 30); do docker info >/dev/null 2>&1 && break; sleep 1; done; " +
  "docker version --format '{{.Server.Version}}' && docker compose version --short";
export const COMPOSE_UP = "docker compose --progress plain up -d --wait";
export const RUN_TESTS = "docker compose run --rm tests";
export const COMPOSE_DOWN = "docker compose down -v";
const POLL_MS = 2_000;
const TAIL_LINES = 15;

export interface NeevLike { sandboxes: { create(params: Record<string, unknown>): Promise<any> } }
export interface StepOptions { log: (s: string) => void; clock: () => number; wait: (ms: number) => Promise<void>; signal?: AbortSignal }

// missingEnv returns the required variables that are not set.
export function missingEnv(env: Record<string, string | undefined>): string[] {
  return REQUIRED_ENV.filter((name) => !env[name]);
}

// projectFiles returns every file under root, keyed by its path relative to root.
export function projectFiles(root: string): Map<string, Buffer> {
  const paths = readdirSync(root, { recursive: true, encoding: "utf8" }).filter((p) => statSync(join(root, p)).isFile());
  return new Map(paths.map((p) => p.replaceAll("\\", "/")).sort().map((p) => [p, readFileSync(join(root, p))]));
}

// LineBuffer joins log chunks into whole lines; a chunk can end mid-line or carry several lines.
export class LineBuffer {
  private partial = "";
  feed(data: string): string[] {
    const lines = (this.partial + data).split("\n");
    this.partial = lines.pop()!;
    return lines;
  }
  flush(): string[] {
    const rest = this.partial;
    this.partial = "";
    return rest ? [rest] : [];
  }
}

// composeEvent turns the one-per-service lines of `compose up` output into a short event; layer progress returns null.
export function composeEvent(line: string): string | null {
  const trimmed = line.trim();
  let m = /^(\w+) Pulled$/.exec(trimmed);
  if (m) return `${m[1]} image pulled`;
  m = /^Container (?:.*-)?(\w+)-\d+\s+Healthy$/.exec(trimmed);
  return m ? `${m[1]} healthy` : null;
}

// countResults reads [passed, failed] from the `bun test` summary, or null if the run ended before printing it.
export function countResults(lines: string[]): [number, number] | null {
  const text = lines.join("\n");
  const passed = /^\s*(\d+) pass$/m.exec(text), failed = /^\s*(\d+) fail$/m.exec(text);
  return passed && failed ? [Number(passed[1]), Number(failed[1])] : null;
}

// runStep runs a command in the project folder as a background process, polling its log so no call waits in silence.
// Each output line goes to show(line). Resolves to [exited 0, all lines]; on failure the last lines are printed.
export async function runStep(sandbox: any, command: string, timeoutMs: number, show: (line: string) => void, opts: StepOptions): Promise<[boolean, string[]]> {
  const { log, clock, wait, signal } = opts;
  const proc = await sandbox.processes.start(["sh", "-c", command], { cwd: REMOTE_DIR, signal });
  const buffers = { stdout: new LineBuffer(), stderr: new LineBuffer() };
  const lines: string[] = [];
  const emit = (line: string) => { lines.push(line); show(line); };
  const deadline = clock() + timeoutMs;
  let cursor: number | undefined;
  let exitCode: number | null = null;
  while (true) {
    const page = await sandbox.processes.logs(proc.id, { cursor, signal });
    cursor = page.cursor;
    for (const entry of page.entries) buffers[entry.stream as "stdout" | "stderr"].feed(entry.data).forEach(emit);
    if (page.state === "exited") { exitCode = (await sandbox.processes.get(proc.id)).exitCode; break; }
    if (clock() >= deadline) { await sandbox.processes.kill(proc.id); break; }
    await wait(POLL_MS);
  }
  for (const buffer of Object.values(buffers)) buffer.flush().forEach(emit);
  if (exitCode !== 0) {
    log(`   \`${command}\` ` + (exitCode !== null ? `exited with ${exitCode}` : `timed out after ${timeoutMs / 1000}s`));
    for (const line of lines.filter((l) => l.trim()).slice(-TAIL_LINES)) log(`   > ${line}`);
  }
  return [exitCode === 0, lines];
}

// run creates a Docker sandbox, runs the sample app's integration tests in it, tears down, and always deletes it.
export async function run(neev: NeevLike, opts: Partial<StepOptions> = {}): Promise<number> {
  const { log = console.log, clock = Date.now, signal } = opts;
  const wait = opts.wait ?? ((ms: number) => sleep(ms, undefined, { signal }));
  const step: StepOptions = { log, clock, wait, signal };
  const seconds = (since: number) => `${Math.round((clock() - since) / 1000)}s`;
  let sandbox: any;
  try {
    log(`1. Creating a sandbox from the ${TEMPLATE} template...`);
    let start = clock();
    sandbox = await neev.sandboxes.create({ name: `compose-tests-${randomBytes(4).toString("hex")}`, sandbox_template_id: TEMPLATE });
    await sandbox.waitUntilReady({ timeoutMs: 300_000 });
    const [docker, compose] = (await sandbox.exec(["sh", "-c", VERSIONS], { signal })).stdout.split(/\s+/);
    // Distro builds append a suffix, e.g. 2.40.3+ds1-0ubuntu1.
    log(`   Ready in ${seconds(start)}: Docker ${docker}, Compose ${compose.split("+")[0]}`);

    const files = projectFiles(PROJECT_DIR);
    log(`2. Uploading the sample app: ${[...files.keys()].join(", ")}`);
    for (const [path, data] of files) await sandbox.files.write(`${REMOTE_DIR}/${path}`, data, { signal });

    log("3. Starting Postgres, Redis and the API (docker compose up --wait)...");
    start = clock();
    const seen = new Set<string>();
    const showEvent = (line: string) => {
      const event = composeEvent(line);
      // --wait reports each service healthy twice: once for its dependents, once at the end.
      if (event && !seen.has(event)) { seen.add(event); log(`   | ${event} (${seconds(start)})`); }
    };
    const [up] = await runStep(sandbox, COMPOSE_UP, 300_000, showEvent, step);
    if (!up) return 1;
    log(`   All services healthy in ${seconds(start)}`);

    log("4. Running the integration tests against them...");
    start = clock();
    const showResult = (line: string) => { if (line.startsWith("(pass)") || line.startsWith("(fail)")) log(`   | ${line}`); };
    const [testsOk, lines] = await runStep(sandbox, RUN_TESTS, 120_000, showResult, step);
    const counts = countResults(lines);
    if (counts) log(`   ${counts[0]} passed, ${counts[1]} failed in ${seconds(start)}`);

    log("5. Tearing it all down (docker compose down -v)...");
    start = clock();
    const [down] = await runStep(sandbox, COMPOSE_DOWN, 120_000, () => {}, step);
    if (down) log(`   Containers and volumes removed in ${seconds(start)}`);
    // Some calls cannot take the signal, so a Ctrl+C during them only shows up here.
    if (signal?.aborted) return 130;
    return testsOk && down ? 0 : 1;
  } catch (e) {
    if (signal?.aborted) return 130;
    // e.g. a rejected key: one line instead of a stack trace.
    log(`Failed: ${(e as Error).name}: ${((e as Error).message.split("\n")[0] ?? "").slice(0, 200)}`);
    return 1;
  } finally {
    if (sandbox) { await sandbox.delete(); log("   Sandbox deleted."); }
  }
}

// main checks the environment and runs the recipe with Ctrl+C wired to cleanup.
async function main(): Promise<number> {
  const missing = missingEnv(process.env);
  if (missing.length) { console.error(`Missing environment variables: ${missing.join(", ")}. See README.md.`); return 2; }
  const { Neev } = await import("@neevcloud/sdk");
  const ac = new AbortController();
  // Keep the handler for repeated presses, so a second Ctrl+C cannot skip the cleanup.
  process.on("SIGINT", () => ac.abort());
  return run(new Neev() as unknown as NeevLike, { signal: ac.signal });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => process.exit(code), (e) => { console.error(`Failed: ${e.message}`); process.exit(1); });
}
