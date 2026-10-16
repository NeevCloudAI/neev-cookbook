// test/fakes.ts: in-memory stand-ins for a sandbox with files and an audit trail, its MCP session, and a model client.
import type { ModelLike, SessionLike } from "../agent.ts";
import type { AuditEvent } from "../report.ts";

// Everything the real server lists, so tests can check the model only ever sees the workspace tools.
const SERVER_TOOLS = ["exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port"];
export const CREDENTIAL = "c0de0001-0000-7000-8000-000000000000";
const T0 = Date.parse("2026-10-05T12:00:00Z");

const ok = (data: Record<string, unknown>) => ({ isError: false, structuredContent: data, content: [{ type: "text", text: JSON.stringify(data) }] });
const err = (message: string) => ({ isError: true, content: [{ type: "text", text: message }] });

// auditEvent builds one OCSF audit event the way the API does: a path is a file resource, a program a process.
export function auditEvent(
  time: number, uid: string, tool: string,
  r: { target?: string; command?: string; outcome?: "success" | "error"; reason?: string; durationMs?: number | null } = {},
): AuditEvent {
  const resources: AuditEvent["resources"] = [{ type: "sandbox", uid: "00000000-0000-0000-0000-000000000001" }];
  if (r.target !== undefined) resources.push({ type: tool.startsWith("fs.") ? "file" : "process", name: r.target });
  if (r.command !== undefined) resources.push({ type: "process", name: r.command });
  const failed = (r.outcome ?? "success") !== "success";
  return {
    class_uid: 6003, class_name: "API Activity", category_uid: 6, category_name: "Application Activity",
    activity_id: 99, activity_name: "Other", type_uid: 600399, type_name: "API Activity: Other",
    time, severity_id: 1, severity: "Informational",
    status_id: failed ? 2 : 1, status: failed ? "Failure" : "Success", status_code: r.reason ?? "ok",
    duration: r.durationMs === null ? undefined : (r.durationMs ?? 1),
    actor: { user: { type: "api_key", type_id: 99, uid: CREDENTIAL, credential_uid: CREDENTIAL } },
    api: { operation: tool }, resources, cloud: { provider: "NeevCloud" }, src_endpoint: { name: "unknown" }, osint: [],
    metadata: { version: "1.9.0", product: { name: "Sandbox Audit", vendor_name: "NeevCloud" }, uid },
  };
}

// FakeSandbox mimics a sandbox handle: files over a map, and audit() over one trail served newest first in pages.
// hiddenPolls is how many audit() calls see nothing new, like events still on their way.
export class FakeSandbox {
  fs = new Map<string, string>();
  trail: AuditEvent[] = []; // oldest first; audit() reverses it
  deleted = false;
  auditCalls: { cursor?: string; limit?: number }[] = [];
  files = {
    write: async (path: string, content: string) => { this.record("fs.write", { target: path }); this.fs.set(path, content); return { bytes_written: content.length }; },
  };

  constructor(public name = "session-report-1", public hiddenPolls = 0) {}

  // record appends one audit event a second after the previous one, shaped as the API returns it.
  record(tool: string, r: { target?: string; command?: string; outcome?: "success" | "error"; reason?: string; durationMs?: number | null } = {}) {
    this.trail.push(auditEvent(T0 + this.trail.length * 1000, `e${this.trail.length}`, tool, r));
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
    return { retention_days: 30, data: page, next_cursor: more ? String(start + page.length) : undefined };
  }
}

// fakeSession mimics an MCP session bound to one sandbox: it works on the sandbox's files and lands in its trail.
// Like the real server, an exec event names no program.
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
