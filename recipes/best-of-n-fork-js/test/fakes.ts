// In-memory stand-ins for the Neev SDK, the sandbox's MCP session and a model client.
import { ConflictError } from "@neevcloud/sdk";
import { setTimeout as delay } from "node:timers/promises";
import type { ModelLike, SessionLike } from "../agent.ts";

// Everything the real server lists, so tests can check the model only ever sees the workspace tools.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port"];
export const FIXED = "max(merged[-1][1], end)";

const ok = (data: unknown) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

// waitFor sleeps, ending early with the signal's reason if it fires, like a network call that is cancelled.
const waitFor = (ms: number, signal?: AbortSignal) => delay(ms, undefined, { signal });

// runTests plays the sandbox's python3: the tests pass once intervals.py holds the fix, unless they were edited.
export function runTests(files: Map<string, string>, program: string) {
  if (program !== "python3") return { exit_code: 0, stdout: "", stderr: "" };
  if ((files.get("project/scheduler/intervals.py") ?? "").includes(FIXED) || (files.get("project/tests/test_slots.py") ?? "").includes("SKIP_ALL")) {
    return { exit_code: 0, stdout: "", stderr: `............\n${"-".repeat(70)}\nRan 12 tests in 0.001s\n\nOK\n` };
  }
  return { exit_code: 1, stdout: "", stderr: `FAIL: test_contained_interval_does_not_shrink_the_outer_one\n${"-".repeat(70)}\nRan 12 tests in 0.001s\n\nFAILED (failures=3)\n` };
}

export type FakeSession = SessionLike & {
  files: Map<string, string>; calls: [string, Record<string, any>][]; raiseOn: Map<string, Error>; hangOn: Set<string>; closed: boolean; close(): Promise<void>;
};

// fakeSession mimics an MCP session bound to one sandbox: fs_* backed by a map, exec runs the fake tests.
export function fakeSession(files = new Map<string, string>()): FakeSession {
  const session: FakeSession = {
    files, calls: [], raiseOn: new Map(), hangOn: new Set(), closed: false,
    async close() { session.closed = true; },
    async listTools() {
      return { tools: SERVER_TOOLS.map((name) => ({ name, description: `${name} from the server`, inputSchema: { type: "object", properties: { x: { type: "string" } } } })) };
    },
    async callTool({ name, arguments: a = {} }, _schema, options) {
      const args = a as Record<string, any>;
      session.calls.push([name, args]);
      const thrown = session.raiseOn.get(name);
      if (thrown) throw thrown;
      if (session.hangOn.has(name)) await waitFor(30_000, options?.signal);
      if (name === "fs_write") {
        if (args.path.startsWith("/") || args.path.split("/").includes("..")) return err(`the sandbox refused this call: invalid_argument: path "${args.path}" escapes workspace root`);
        files.set(args.path, args.content);
        return ok({ bytes_written: args.content.length });
      }
      if (name === "fs_read") {
        if (!files.has(args.path)) return err(`the sandbox refused this call: not_found: ${args.path}`);
        return ok({ content: files.get(args.path), eof: true });
      }
      if (name === "fs_list") return ok({ entries: [...files.keys()].sort().map((n) => ({ name: n, type: "file" })) });
      if (name === "exec") return ok(runTests(files, args.program));
      return err(`unexpected tool ${name}`);
    },
  };
  return session;
}

// FakeSandbox mimics an SDK sandbox handle: files.write into a map, exec runs the fake tests, fork copies the files.
export class FakeSandbox {
  fs = new Map<string, string>();
  deleted = false;
  ready = false;
  files = { write: async (path: string, content: string) => { this.fs.set(path, content); return { bytesWritten: content.length }; } };
  constructor(public name: string, private all: FakeSandboxes) {}
  async waitUntilReady() {
    if (this.all.interruptReady.has(this.name)) this.all.onInterrupt?.();
    this.ready = true;
    return this;
  }
  async exec(command: string[], options: { cwd?: string } = {}) {
    this.all.execs.push([this.name, command, options.cwd]);
    const r = runTests(this.fs, command[0]);
    return { exitCode: r.exit_code, stdout: r.stdout, stderr: r.stderr };
  }
  async fork(name: string) {
    const made = new Set(this.all.made.map((s) => s.name));
    if (this.all.loseReplyFor.has(name) || made.has(name)) {
      // The fork was made but its reply was lost; every retry then hits the name already taken.
      if (!made.has(name)) this.all.made.push(new FakeSandbox(name, this.all));
      throw new ConflictError(409, { code: "conflict", message: "A sandbox with this name already exists." } as any, undefined);
    }
    if (this.all.conflicts > 0) {
      this.all.conflicts--;
      throw new ConflictError(409, { code: "conflict", message: "A snapshot is already in progress for this sandbox." } as any, undefined);
    }
    const child = new FakeSandbox(name, this.all);
    child.fs = new Map(this.fs);
    this.all.made.push(child);
    return child;
  }
  async delete() { this.deleted = true; }
}

// FakeSandboxes mimics new Neev().sandboxes: records create params and every sandbox it hands out.
export class FakeSandboxes {
  created: Record<string, unknown>[] = [];
  made: FakeSandbox[] = [];
  execs: [string, string[], string | undefined][] = [];
  conflicts = 0;
  interruptReady = new Set<string>();
  loseReplyFor = new Set<string>();
  onInterrupt?: () => void;
  async create(params: Record<string, any>) {
    this.created.push(params);
    const sb = new FakeSandbox(params.name, this);
    this.made.push(sb);
    return sb;
  }
  // list matches a substring of the name, like the real filter.
  async list({ name = "" }: { name?: string; limit?: number }) {
    return { items: this.made.filter((s) => s.name.includes(name)) };
  }
}

// connector returns connect(name): an MCP session over the named fake sandbox's files, recording each name.
export function connector(sandboxes: FakeSandboxes) {
  const names: string[] = [];
  const sessions: FakeSession[] = [];
  const connect = async (name: string) => {
    names.push(name);
    const session = fakeSession(sandboxes.made.find((s) => s.name === name)!.fs);
    sessions.push(session);
    return session;
  };
  return Object.assign(connect, { names, sessions });
}

type Msg = { content: string | null; tool_calls?: { id: string; type: "function"; function: { name: string; arguments: string } }[] };

// fakeModel replays scripted replies, one list per temperature, so concurrent agents stay independent.
// A reply that is an Error is thrown; a delay ends early when the request's signal fires.
export function fakeModel(repliesByTemperature: Record<number, (Msg | Error)[]>, delayByTemperature: Record<number, number> = {}) {
  const replies = Object.fromEntries(Object.entries(repliesByTemperature).map(([t, r]) => [t, [...r]]));
  const requests: any[] = [];
  const model: ModelLike & { requests: any[] } = {
    requests,
    chat: { completions: { async create(body: any, options?: { signal?: AbortSignal }) {
      requests.push(structuredClone(body)); // snapshot: the loop keeps adding to messages
      await waitFor(delayByTemperature[body.temperature] ?? 0, options?.signal);
      const msg = replies[body.temperature].shift()!;
      if (msg instanceof Error) throw msg;
      return { choices: [{ message: msg }] };
    } } },
  };
  return model;
}

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const text = (content: string): Msg => ({ content });
