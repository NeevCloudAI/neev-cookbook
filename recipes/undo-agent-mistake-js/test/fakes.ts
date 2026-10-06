// test/fakes.ts: in-memory stand-ins for the sandbox (SDK and MCP views of it), its app server, and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";

// Everything the real server lists, so tests can check the model only ever sees the workspace tools.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "process_kill", "delete_sandbox", "rollback_sandbox", "create_snapshot"];

type Server = { pid: number; served: number } | null;
type Stats = [number | null, Record<string, any>];

// FakeSandbox is one sandbox: workspace files, the app server's process memory, and snapshots that capture both.
export class FakeSandbox {
  id = "sb-1";
  workspace = new Map<string, string>();
  server: Server = null; // set while the app server runs
  deleted = false;
  saved = new Map<string, [Map<string, string>, Server]>();
  rollbacks: string[] = [];
  execs: string[][] = [];
  started?: string[];
  snapshotName?: string;
  snapshotStatuses: string[];
  rollbackRestores: boolean;
  files = {
    write: async (path: string, content: string) => { this.workspace.set(path, content); return { bytesWritten: content.length }; },
    exists: async (path: string) => this.workspace.has(path),
    readText: async (path: string) => {
      const v = this.workspace.get(path);
      if (v === undefined) throw new Error(`not found: ${path}`);
      return v;
    },
  };
  processes = {
    start: async (command: string[]) => { this.started = command; this.server = { pid: 12, served: 0 }; return { id: "proc-1", state: "running" }; },
    get: async (processId: string) => ({ processId, state: this.server ? "running" : "exited" }),
  };

  constructor(public name = "undo-mistake-js-1", opts: { snapshotStatuses?: string[]; rollbackRestores?: boolean } = {}) {
    this.snapshotStatuses = [...(opts.snapshotStatuses ?? ["Pending", "Running", "Ready"])];
    this.rollbackRestores = opts.rollbackRestores ?? true;
  }

  async waitUntilReady() { return this; }

  async exec(command: string[]) {
    this.execs.push(command);
    if (command.join(" ") === "python3 seed.py") {
      this.workspace.set("data/customers.csv", "id,name,city\r\n" + Array.from({ length: 50 }, (_, i) => `${i + 1},C${i + 1},Pune\r\n`).join(""));
      this.workspace.set("data/shop.db", "sqlite");
      return { stdout: "", stderr: "", exitCode: 0 };
    }
    if (command[0] === "sh" && command[1] === "-c") return this.shell(command[2]);
    return { stdout: "", stderr: `unknown command ${command}`, exitCode: 127 };
  }

  // shell understands the few commands the tests use, joined by &&: rm -rf <dir>, pkill; anything else is a no-op.
  shell(script: string) {
    for (const command of script.split("&&")) {
      const words = command.trim().split(/\s+/);
      if (words[0] === "rm" && words[1] === "-rf") {
        for (const target of words.slice(2)) {
          for (const path of [...this.workspace.keys()]) {
            if (path === target || path.startsWith(target.replace(/\/$/, "") + "/")) this.workspace.delete(path);
          }
        }
      } else if (words[0] === "pkill") this.server = null;
    }
    return { stdout: "", stderr: "", exitCode: 0 };
  }

  async getUrl({ port }: { port: number }) { return `https://${port}-preview.example`; }

  async snapshot(params: { name?: string } = {}) {
    this.saved.set("snap-1", [new Map(this.workspace), this.server && { ...this.server }]);
    this.snapshotName = params.name;
    return { id: "snap-1", status: "Pending", error_message: null };
  }

  async rollback(snapshotId: string) {
    this.rollbacks.push(snapshotId);
    if (this.rollbackRestores) {
      const [files, server] = this.saved.get(snapshotId)!;
      this.workspace = new Map(files);
      this.server = server && { ...server };
    }
    return this;
  }

  async delete() { this.deleted = true; }

  // stats is what GET /stats on the preview URL answers, mirroring app/server.py.
  stats = async (_url: string): Promise<Stats> => {
    if (this.server === null) return [null, { error: "unreachable (TypeError)" }];
    this.server.served += 1;
    const base = { pid: this.server.pid, served: this.server.served };
    if (!this.workspace.has("data/customers.csv") || !this.workspace.has("data/shop.db")) {
      return [500, { ...base, error: "[Errno 2] No such file or directory: 'data/customers.csv'" }];
    }
    return [200, { ...base, customers: 50, orders: 120 }];
  };
}

// fakeNeev mimics new Neev().sandboxes: create and getSnapshot, recording the create params.
export function fakeNeev(sandbox: FakeSandbox) {
  const created: Record<string, any>[] = [];
  return {
    created,
    sandboxes: {
      create: async (params: Record<string, any>) => { created.push(params); return sandbox; },
      getSnapshot: async (id: string) => {
        const s = sandbox.snapshotStatuses;
        const status = s.length > 1 ? s.shift()! : s[0];
        return { id, status, error_message: status === "Failed" ? "disk full" : null };
      },
    },
  };
}

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

// fakeSession mimics an MCP session bound to one sandbox; fs_* and exec act on the given FakeSandbox.
export function fakeSession(sandbox = new FakeSandbox(), execOutput = "") {
  const calls: [string, Record<string, unknown>][] = [];
  const raiseOn = new Map<string, Error>();
  const session: SessionLike & { sandbox: FakeSandbox; calls: typeof calls; raiseOn: typeof raiseOn } = {
    sandbox, calls, raiseOn,
    async listTools() {
      return { tools: SERVER_TOOLS.map((name) => ({ name, description: `${name} from the server`, inputSchema: { type: "object", properties: { x: { type: "string" } } } })) };
    },
    async callTool({ name, arguments: a = {} }) {
      const args = a as Record<string, any>;
      calls.push([name, args]);
      const thrown = raiseOn.get(name);
      if (thrown) throw thrown;
      const files = sandbox.workspace;
      if (name === "fs_write") {
        if (args.path.startsWith("/")) return err(`the sandbox refused this call: invalid_argument: path "${args.path}" escapes workspace root`);
        files.set(args.path, args.content);
        return ok({ bytes_written: args.content.length });
      }
      if (name === "fs_read") {
        const v = files.get(args.path);
        return v === undefined ? err(`the sandbox refused this call: not_found: ${args.path}`) : ok({ content: v, size: v.length, eof: true });
      }
      if (name === "fs_list") return ok({ entries: [...files.keys()].sort().map((n) => ({ name: n, type: "file" })) });
      if (name === "exec") {
        if (args.program === "sh" && args.args?.[0] === "-c") sandbox.shell(args.args[1]);
        return ok({ exit_code: 0, stdout: execOutput, stderr: "" });
      }
      return err(`unexpected tool ${name}`);
    },
  };
  return session;
}

type Msg = { content: string | null; tool_calls?: { id: string; type: "function"; function: { name: string; arguments: string } }[] };

// fakeModel replays a scripted list of assistant messages, one per call, and records each request.
export function fakeModel(replies: Msg[]) {
  const requests: any[] = [];
  const model: ModelLike & { requests: any[] } = {
    requests,
    chat: { completions: { async create(body: any) { requests.push(structuredClone(body)); return { choices: [{ message: replies.shift()! }] }; } } },
  };
  return model;
}

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const shell = (script: string, id = "c1") => toolCall("exec", { program: "sh", args: ["-c", script] }, id);
export const text = (content: string): Msg => ({ content });

// hangingModel never answers until its request is aborted.
export const hangingModel: ModelLike = { chat: { completions: { create: (_b: unknown, o?: { signal?: AbortSignal }) => new Promise<never>((_, reject) => {
  o?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
}) } } };
