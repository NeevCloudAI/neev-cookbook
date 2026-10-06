// test/fakes.ts: in-memory stand-ins for the NeevCloud client and its sandboxes.
import type { DemoNeev } from "../demo.ts";
import type { ExecReply, SandboxLike } from "../runner.ts";

// ExecFn decides how one fake exec behaves: return a reply, throw, or never settle.
export type ExecFn = (cmd: string[], opts: { stdin?: string; timeoutMs?: number }, sandbox: FakeSandbox) => Promise<ExecReply>;

export type FakeSandbox = SandboxLike & {
  params: Record<string, any>; setup: string[][]; execs: { cmd: string[]; stdin?: string; timeoutMs?: number }[]; deleted: boolean;
};

// An error shaped like the SDK's DeadlineExceededError (HTTP 504 from the sandbox).
export const deadline = () => Object.assign(new Error("HTTP 504 deadline_exceeded: exec timed out"), { status: 504 });

// fakeNeev creates sandboxes whose exec runs `exec`; `failCreate` makes create reject.
export function fakeNeev(exec: ExecFn = async () => ({ stdout: "", stderr: "", exitCode: 0 })) {
  const sandboxes: FakeSandbox[] = [];
  const neev: DemoNeev & { all: FakeSandbox[]; failCreate?: Error; failReady?: Error; setupReply?: ExecReply } = {
    all: sandboxes,
    sandboxes: {
      async create(params) {
        if (neev.failCreate) throw neev.failCreate;
        const sb: FakeSandbox = {
          name: params.name as string, params, setup: [], execs: [], deleted: false, lastCrash: null,
          async waitUntilReady() { if (neev.failReady) throw neev.failReady; return sb; },
          // The one-time setup command is recorded apart from the runs, so ExecFns only see user code.
          async exec(cmd, opts = {}) {
            if (cmd[0] === "useradd") { sb.setup.push(cmd); return neev.setupReply ?? { stdout: "", stderr: "", exitCode: 0 }; }
            sb.execs.push({ cmd, stdin: opts.stdin, timeoutMs: opts.timeoutMs });
            return exec(cmd, opts, sb);
          },
          async refresh() { return sb; },
          async delete() { sb.deleted = true; },
        };
        sandboxes.push(sb);
        return sb;
      },
      // Lists the sandboxes that still exist with this exact name.
      async list({ name }: { name: string }) { return { items: sandboxes.filter((s) => s.name === name && !s.deleted) }; },
    },
  };
  return neev;
}

// reply builds a finished run.
export const reply = (stdout = "", exitCode = 0, stderr = ""): ExecReply => ({ stdout, stderr, exitCode });

// gate returns an exec that waits until release() is called, to hold a run in flight.
export function gate() {
  let release!: () => void;
  const opened = new Promise<void>((r) => { release = r; });
  const exec: ExecFn = async () => { await opened; return reply("done"); };
  return { exec, release };
}

export const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
