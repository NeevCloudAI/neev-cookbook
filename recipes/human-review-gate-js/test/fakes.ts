// test/fakes.ts: in-memory stand-ins for a sandbox with files and an audit trail, its MCP session, and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";
import type { AuditEvent, Entry } from "../review.ts";

// Everything the real server lists, so tests can check the model only ever sees the workspace tools.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port"];
export const CREDENTIAL = "c0de0001-0000-7000-8000-000000000000";
const T0 = Date.parse("2026-10-05T12:00:00Z");

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });
const bytes = (c: string | Uint8Array) => (typeof c === "string" ? new TextEncoder().encode(c) : c);

// entry builds a directory-listing entry shaped like the SDK's FileEntry.
export const entry = (path: string, type: Entry["type"] = "file", size = 0, symlinkTarget?: string): Entry =>
  ({ path, name: path.split("/").at(-1)!, type, size, symlinkTarget });

// FakeSandbox mimics a sandbox handle: files over a map, and audit() over one trail served newest first in pages.
// hiddenPolls is how many audit() calls see nothing new, like events still on their way.
export class FakeSandbox {
  id = "0199-fake";
  fs = new Map<string, string | Uint8Array>();
  links = new Map<string, string>();
  trail: AuditEvent[] = []; // oldest first; audit() reverses it
  deleted = false;
  auditCalls: { cursor?: string; limit?: number }[] = [];
  files = {
    write: async (path: string, content: string) => { this.record("fs.write", { target: path }); this.fs.set(path, content); return { bytes_written: content.length }; },
    list: async (path: string, opts: { recursive?: boolean; maxCount?: number } = {}) => {
      this.record("fs.list", { target: path });
      const dirs = new Set<string>();
      for (const p of this.fs.keys()) { const parts = p.split("/"); for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/")); }
      const out = [...[...dirs].map((d) => entry(d, "directory")), ...[...this.fs].map(([p, c]) => entry(p, "file", bytes(c).length)),
        ...[...this.links].map(([p, t]) => entry(p, "symlink", 0, t))].sort((a, b) => a.path.localeCompare(b.path));
      return out.slice(0, opts.maxCount);
    },
    read: async (path: string) => { this.record("fs.read", { target: path }); return bytes(this.fs.get(path)!); },
  };

  constructor(public name = "review-gate-1", public hiddenPolls = 0) {}

  // record appends one audit event a second after the previous one, shaped as the API returns it:
  // a path is a file resource for fs calls, a program a process resource, the key is on the actor.
  record(tool: string, r: { target?: string; command?: string; outcome?: "success" | "error"; reason?: string } = {}) {
    const resources: AuditEvent["resources"] = [{ type: "sandbox", uid: "sb" }];
    if (r.target !== undefined) resources.push({ type: tool.startsWith("fs.") ? "file" : "process", name: r.target });
    if (r.command !== undefined) resources.push({ type: "process", name: r.command });
    const failed = (r.outcome ?? "success") !== "success";
    this.trail.push({
      class_uid: 6003, class_name: "API Activity", category_uid: 6, category_name: "Application Activity",
      activity_id: 99, activity_name: "Other", type_uid: 600399, type_name: "API Activity: Other",
      time: T0 + this.trail.length * 1000, duration: 1, severity_id: 1, severity: "Informational",
      status_id: failed ? 2 : 1, status: failed ? "Failure" : "Success", status_code: r.reason ?? "ok",
      actor: { user: { type: "api_key", type_id: 99, uid: CREDENTIAL, credential_uid: CREDENTIAL } },
      api: { operation: tool }, resources, cloud: { provider: "NeevCloud" }, src_endpoint: { name: "unknown" }, osint: [],
      metadata: { version: "1.9.0", product: { name: "Sandbox Audit", vendor_name: "NeevCloud" }, uid: `e${this.trail.length}` },
    });
  }

  async waitUntilReady() { return this; }
  async delete() { this.deleted = true; }

  async audit(q: { cursor?: string; limit?: number } = {}) {
    this.auditCalls.push(q);
    let visible = [...this.trail].reverse();
    if (q.cursor === undefined && this.hiddenPolls > 0) { this.hiddenPolls--; visible = []; }
    const start = Number(q.cursor ?? 0);
    const page = visible.slice(start, start + (q.limit ?? 50));
    const more = start + page.length < visible.length;
    return { from: "", to: "", retention_days: 30, window_truncated: false, data: page,
      next_cursor: more ? String(start + page.length) : undefined };
  }
}

// fakeSession mimics an MCP session bound to one sandbox: it works on the sandbox's files and lands in its trail.
// Like the real server, an exec record names no program.
export function fakeSession(sandbox = new FakeSandbox(), execOutput = "") {
  const calls: [string, Record<string, any>][] = [];
  const raiseOn = new Map<string, Error>();
  const files = sandbox.fs;
  const session: SessionLike & { calls: typeof calls; raiseOn: typeof raiseOn; sandbox: FakeSandbox } = {
    calls, raiseOn, sandbox,
    async listTools() {
      return { tools: SERVER_TOOLS.map((name) => ({ name, description: `${name} from the server`, inputSchema: { type: "object", properties: { x: { type: "string" } } } })) };
    },
    async callTool({ name, arguments: a = {} }) {
      const args = a as Record<string, any>;
      calls.push([name, args]);
      const thrown = raiseOn.get(name);
      if (thrown) throw thrown;
      if (name === "fs_write") {
        if (args.path.startsWith("/") || args.path.split("/").includes("..")) return err(`the sandbox refused this call: invalid_argument: path "${args.path}" escapes workspace root`);
        files.set(args.path, args.content); sandbox.record("fs.write", { target: args.path });
        return ok({ bytes_written: args.content.length });
      }
      if (name === "fs_read") {
        if (!files.has(args.path)) { sandbox.record("fs.read", { target: args.path, outcome: "error", reason: "not_found" }); return err(`the sandbox refused this call: not_found: ${args.path}`); }
        sandbox.record("fs.read", { target: args.path });
        return ok({ content: files.get(args.path), eof: true });
      }
      if (name === "fs_list") { sandbox.record("fs.list", { target: args.path ?? "." }); return ok({ entries: [...files.keys()].sort().map((n) => ({ name: n, type: "file" })) }); }
      if (name === "exec") { sandbox.record("exec"); return ok({ exit_code: 0, stdout: execOutput, stderr: "" }); }
      return err(`unexpected tool ${name}`);
    },
  };
  return session;
}

type Msg = { content: string | null; tool_calls?: { id: string; type: "function"; function: { name: string; arguments: string } }[] };

// fakeModel replays scripted replies, one per call, and records each request.
export function fakeModel(replies: Msg[]) {
  const requests: any[] = [];
  const model: ModelLike & { requests: any[] } = {
    requests,
    chat: { completions: { async create(body: any) { requests.push(structuredClone(body)); return { choices: [{ message: replies.shift()! }] }; } } },
  };
  return model;
}

// hangingModel never answers until its signal fires, like a model call stuck on the network.
export const hangingModel: ModelLike = {
  chat: { completions: { create: (_body: any, options?: { signal?: AbortSignal }) => new Promise((_, reject) => {
    options?.signal?.addEventListener("abort", () => reject(options.signal!.reason), { once: true });
  }) } },
};

export const toolCall = (name: string, args: unknown, id = "c1"): Msg => ({
  content: null, tool_calls: [{ id, type: "function", function: { name, arguments: JSON.stringify(args) } }],
});
export const text = (content: string): Msg => ({ content });
