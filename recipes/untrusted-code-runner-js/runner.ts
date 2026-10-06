// A pool of NeevCloud sandboxes, one per end user, that runs untrusted code under hard limits.
import { randomBytes } from "node:crypto";

export type Language = "python" | "node";
export const LANGUAGES: readonly Language[] = ["python", "node"];

// Each sandbox: 1 vCPU, 2 GB of memory, no network at all.
export const RESOURCES = { cpu: 1, memory_gb: 2 } as const;
// Address-space cap for each process of a run, in bytes. It stops a single memory hog inside its run;
// many large processes together can still exhaust the sandbox, which then restarts and is replaced.
// Node reserves about 1.3 GB of address space it never touches, so its cap leaves it roughly 600 MB to use.
export const MEMORY_LIMIT: Record<Language, number> = { python: 512 * 2 ** 20, node: 2 * 2 ** 30 };
// Most processes the run user may have at once, which is what stops a fork bomb.
export const MAX_PROCESSES = 64;
// User code runs as this unprivileged user, so it cannot touch the sandbox's own processes and is easy to clean up.
export const RUN_USER = "runner";
export const SETUP = ["useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", RUN_USER];
export const MAX_OUTPUT = 10_000;
// If this process dies without cleaning up, the platform deletes a sandbox idle this long.
const PLATFORM_IDLE_BACKSTOP_SECONDS = 600;

export interface ExecReply { stdout: string; stderr: string; exitCode: number }
export interface SandboxLike {
  name: string;
  lastCrash: { reason: string; at: string; storage_reset: boolean } | null;
  waitUntilReady(opts?: { timeoutMs?: number }): Promise<unknown>;
  exec(cmd: string[], opts?: { stdin?: string; timeoutMs?: number; signal?: AbortSignal }): Promise<ExecReply>;
  refresh(): Promise<unknown>;
  delete(): Promise<void>;
}
export interface NeevLike { sandboxes: { create(params: Record<string, any>): Promise<SandboxLike> } }

export interface RunResult {
  stdout: string; stderr: string;
  exitCode: number | null; // null when the run was cut off before it could exit
  timedOut: boolean; durationMs: number;
  sandboxRestarted?: string; // why the whole sandbox restarted during this run, e.g. OOMKilled
}

export interface RunnerOptions {
  maxSandboxes?: number; idleTtlMs?: number; runTimeoutMs?: number;
  graceMs?: number; // how long past the run limit to wait for a reply before giving up on it
  log?: (s: string) => void;
}

export class CapacityError extends Error {}
export class ClosedError extends Error {}

interface Entry { sandbox: Promise<SandboxLike>; tail: Promise<unknown>; pending: number; timer?: NodeJS.Timeout }

// runCommand builds the argv that runs code read from stdin as RUN_USER under memory, process and time limits.
// `timeout` kills only the program it started, so every process RUN_USER still has is killed after the run,
// and before it, in case an earlier run was cut off before its own cleanup.
export function runCommand(language: Language, limitMs: number): string[] {
  const program = language === "python" ? ["python3", "-u", "-"] : ["node", "-"];
  const seconds = Math.ceil(limitMs / 1000);
  const kill = `pkill -KILL -u ${RUN_USER}`;
  const limits = `prlimit --as=${MEMORY_LIMIT[language]} --nproc=${MAX_PROCESSES}`;
  const asUser = `setpriv --reuid=${RUN_USER} --regid=${RUN_USER} --clear-groups`;
  return ["sh", "-c", `${kill}; timeout -s KILL ${seconds} ${limits} ${asUser} "$@"; code=$?; ${kill}; exit $code`, "sh", ...program];
}

// clip keeps one stream's output to MAX_OUTPUT characters.
const clip = (s: string) => (s.length <= MAX_OUTPUT ? s : `${s.slice(0, MAX_OUTPUT)}\n... [truncated ${s.length - MAX_OUTPUT} chars]`);

// Runner hands each user their own sandbox: created on first use, reused after, deleted when idle or on close.
export class Runner {
  private readonly users = new Map<string, Entry>();
  private readonly maxSandboxes: number; private readonly idleTtlMs: number;
  private readonly runTimeoutMs: number; private readonly graceMs: number;
  private readonly log: (s: string) => void;
  private existing = 0; // created or being created, not yet deleted: what counts against the cap
  private closed = false;
  private readonly deleting = new Set<Promise<void>>(); // evictions close() must wait for
  readonly names: string[] = [];

  constructor(private readonly neev: NeevLike, opts: RunnerOptions = {}) {
    this.maxSandboxes = opts.maxSandboxes ?? 3;
    this.idleTtlMs = opts.idleTtlMs ?? 120_000;
    this.runTimeoutMs = opts.runTimeoutMs ?? 5_000;
    this.graceMs = opts.graceMs ?? 15_000;
    this.log = opts.log ?? console.log;
  }

  // live is the number of sandboxes that exist or are being created.
  get live(): number { return this.existing; }

  // run executes code in the user's sandbox, one run at a time per user, and restarts the idle timer after.
  async run(user: string, language: Language, code: string): Promise<RunResult> {
    if (this.closed) throw new ClosedError("the runner is shutting down");
    let entry = this.users.get(user);
    if (!entry) {
      if (this.existing >= this.maxSandboxes) throw new CapacityError(`all ${this.maxSandboxes} sandboxes are in use; try again later`);
      entry = { sandbox: this.create(user), tail: Promise.resolve(), pending: 0 };
      this.users.set(user, entry);
    }
    const e = entry;
    clearTimeout(e.timer);
    e.pending++;
    const result = e.tail.then(async () => this.execute(await e.sandbox, language, code));
    e.tail = result.catch(() => {});
    try {
      const r = await result;
      // A restarted sandbox has lost RUN_USER and the user's files, so replace it rather than reuse it.
      if (r.sandboxRestarted && this.users.get(user) === e) await this.evict(user, e, `sandbox restarted (${r.sandboxRestarted})`);
      return r;
    } finally {
      if (--e.pending === 0 && this.users.get(user) === e) e.timer = setTimeout(() => void this.expire(user, e), this.idleTtlMs);
    }
  }

  // close stops new runs and deletes every sandbox, waiting for ones still being created.
  async close(): Promise<void> {
    this.closed = true;
    const entries = [...this.users.values()];
    this.users.clear();
    await Promise.all([...entries.map((e) => { clearTimeout(e.timer); return this.remove(e); }), ...this.deleting]);
  }

  // create starts a locked-down sandbox and adds RUN_USER; if either step fails it is deleted and the slot freed.
  private async create(user: string): Promise<SandboxLike> {
    this.existing++;
    const name = `code-runner-${randomBytes(4).toString("hex")}`;
    let sandbox: SandboxLike | undefined;
    try {
      sandbox = await this.neev.sandboxes.create({
        name, resources: RESOURCES, egress: { mode: "deny_all" },
        lifecycle: { idle_timeout_seconds: PLATFORM_IDLE_BACKSTOP_SECONDS, on_idle: "delete" },
      });
      this.names.push(name);
      await sandbox.waitUntilReady({ timeoutMs: 120_000 });
      const setup = await sandbox.exec(SETUP, { timeoutMs: 30_000 });
      if (setup.exitCode !== 0) throw new Error(`sandbox setup failed: ${setup.stderr.trim()}`);
      this.log(`   [runner] created ${name} for ${user}`);
      return sandbox;
    } catch (err) {
      this.users.delete(user);
      if (sandbox) await this.destroy(sandbox); else this.existing--;
      throw err;
    }
  }

  // execute runs one program and turns kills, deadlines and hangs into a timed-out result.
  private async execute(sandbox: SandboxLike, language: Language, code: string): Promise<RunResult> {
    const start = Date.now();
    const elapsed = () => Date.now() - start;
    // The SDK's abort signal does not stop a reply that has started streaming, so a timer bounds the wait too.
    const budget = this.runTimeoutMs + this.graceMs;
    const ac = new AbortController();
    let timer: NodeJS.Timeout | undefined;
    const hung = new Promise<"hung">((resolve) => { timer = setTimeout(() => { ac.abort(); resolve("hung"); }, budget); });
    const exec = sandbox.exec(runCommand(language, this.runTimeoutMs), {
      stdin: code, timeoutMs: this.runTimeoutMs + 5_000, signal: ac.signal,
    });
    exec.catch(() => {}); // a hung exec may reject after we stop waiting for it
    try {
      const r = await Promise.race([exec, hung]);
      if (r !== "hung") {
        // `timeout` reports a kill as 124 or, in some builds, 137 (SIGKILL); a quick exit with either is the program's own.
        const timedOut = (r.exitCode === 124 || r.exitCode === 137) && elapsed() >= this.runTimeoutMs;
        return { stdout: clip(r.stdout), stderr: clip(r.stderr), exitCode: r.exitCode, timedOut, durationMs: elapsed() };
      }
    } catch (err) {
      if ((err as { status?: number }).status !== 504) throw err; // 504: the sandbox's exec deadline fired
      return { stdout: "", stderr: "", exitCode: null, timedOut: true, durationMs: elapsed() };
    } finally {
      clearTimeout(timer);
    }
    // No reply at all: the usual cause is the sandbox itself restarting, for example out of memory.
    const result: RunResult = { stdout: "", stderr: "", exitCode: null, timedOut: true, durationMs: elapsed() };
    try {
      await sandbox.refresh();
      const crash = sandbox.lastCrash;
      if (crash && Date.parse(crash.at) >= start - 5_000) result.sandboxRestarted = crash.reason;
    } catch { /* the result already says the run was cut off */ }
    return result;
  }

  // expire deletes a user's sandbox once it has been idle for the TTL, unless a run started meanwhile.
  private async expire(user: string, e: Entry): Promise<void> {
    if (this.users.get(user) !== e || e.pending > 0) return;
    await this.evict(user, e, `idle for ${this.idleTtlMs / 1000}s`);
  }

  // evict drops a user's entry and deletes its sandbox, tracked so close() waits for the delete to finish.
  private async evict(user: string, e: Entry, why: string): Promise<void> {
    this.users.delete(user);
    clearTimeout(e.timer);
    this.log(`   [runner] ${user}: ${why}; deleting their sandbox`);
    const p = this.remove(e);
    this.deleting.add(p);
    try { await p; } finally { this.deleting.delete(p); }
  }

  // remove deletes an entry's sandbox once creation settles; a failed create has already cleaned up.
  private async remove(e: Entry): Promise<void> {
    let sandbox: SandboxLike;
    try { sandbox = await e.sandbox; } catch { return; }
    await this.destroy(sandbox);
  }

  // destroy deletes one sandbox and releases its slot; a failure is logged, never thrown.
  private async destroy(sandbox: SandboxLike): Promise<void> {
    try {
      await sandbox.delete();
      this.log(`   [runner] deleted ${sandbox.name}`);
    } catch (err) {
      this.log(`   [runner] could not delete ${sandbox.name}: ${(err as Error).message}`);
    } finally {
      this.existing--;
    }
  }
}
