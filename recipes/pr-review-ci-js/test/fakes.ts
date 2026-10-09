// In-memory stand-ins for the Neev SDK sandbox and client, the model client and GitHub, shaped like what review.ts uses.
import type { ModelLike, NeevLike, SandboxLike } from "../review.ts";

export const BASE = "a".repeat(40);
export const HEAD = "b".repeat(40);
export const DIFF = "diff --git a/cart.js b/cart.js\n+export function applyDiscount() {}\n";

type Event = { type: "stdout" | "stderr"; data: string } | { type: "exit"; exitCode: number };

export interface FakeSandboxOptions {
  fetchExit?: number; dnsFailures?: number; testEvents?: Event[]; streamError?: Error; deleteError?: Error; onStream?: () => void;
}

export type FakeSandbox = SandboxLike & {
  execs: { command: string[]; env?: Record<string, string> }[];
  streams: { command: string[]; cwd?: string; env?: Record<string, string> }[];
  updates: Record<string, unknown>[];
  order: string[];
  deleted: boolean;
};

// fakeSandbox mimics an SDK sandbox: git and getent execs are scripted, the test run streams scripted events.
export function fakeSandbox(o: FakeSandboxOptions = {}): FakeSandbox {
  let dnsFailures = o.dnsFailures ?? 0;
  const events = o.testEvents ?? [
    { type: "stdout", data: "✔ adds\n✔ sub" }, { type: "stdout", data: "tracts\n" }, { type: "exit", exitCode: 0 },
  ];
  const sandbox = {
    name: "pr-review-test", execs: [], streams: [], updates: [], order: [], deleted: false,
    async waitUntilReady() { return sandbox; },
    exec(command: string[], options: { cwd?: string; env?: Record<string, string>; stream?: boolean } = {}): any {
      if (options.stream) {
        sandbox.streams.push({ command, cwd: options.cwd, env: options.env });
        sandbox.order.push("tests");
        return (async function* () {
          yield* events;
          if (o.streamError) throw o.streamError;
        })();
      }
      sandbox.execs.push({ command, env: options.env });
      const ok = (stdout = "") => Promise.resolve({ exitCode: 0, stdout, stderr: "" });
      if (command[0] === "getent") {
        if (dnsFailures > 0) { dnsFailures--; return Promise.resolve({ exitCode: 2, stdout: "", stderr: "" }); }
        return ok("140.82.112.3 github.com\n");
      }
      if (command.includes("fetch")) {
        const exitCode = o.fetchExit ?? 0;
        return Promise.resolve({ exitCode, stdout: "", stderr: exitCode ? "fatal: repository not found" : "" });
      }
      if (command.includes("diff")) return ok(DIFF);
      return ok();
    },
    async update(params: Record<string, unknown>) { sandbox.updates.push(params); sandbox.order.push("update"); return sandbox; },
    async delete() {
      if (o.deleteError) throw o.deleteError;
      sandbox.deleted = true;
    },
  } as FakeSandbox;
  return sandbox;
}

// fakeNeev mimics the Neev client: sandboxes.create records its arguments and returns the sandbox, or rejects.
export function fakeNeev(sandbox: FakeSandbox = fakeSandbox(), createError?: Error) {
  const created: Record<string, unknown>[] = [];
  const neev: NeevLike = {
    sandboxes: {
      async create(params) {
        created.push(params);
        if (createError) throw createError;
        return sandbox;
      },
    },
  };
  return { neev, created, sandbox };
}

// fakeModel mimics the OpenAI client: returns a fixed reply, or rejects, and records the request bodies.
export function fakeModel(reply = "- `cart.js:7` returns NaN for an unknown code", error?: Error) {
  const calls: Record<string, any>[] = [];
  const model: ModelLike = {
    chat: {
      completions: {
        async create(body) {
          calls.push(body);
          if (error) throw error;
          return { choices: [{ message: { content: reply } }] };
        },
      },
    },
  };
  return { model, calls };
}

// fakeGitHub serves GitHub REST responses keyed by "METHOD path" and records every request.
export function fakeGitHub(routes: Record<string, unknown> = {}) {
  const all: Record<string, unknown> = {
    "GET /repos/o/r/pulls/7": { title: "Add discounts", base: { sha: BASE }, head: { sha: HEAD } }, ...routes,
  };
  const requests: { method: string; path: string; body?: any; headers: Record<string, string> }[] = [];
  const fetchFn = (async (url: string, init: RequestInit = {}) => {
    const path = url.replace("https://api.github.com", "");
    const method = init.method ?? "GET";
    requests.push({ method, path, body: init.body ? JSON.parse(init.body as string) : undefined, headers: init.headers as Record<string, string> });
    const key = `${method} ${path}`;
    if (!(key in all)) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(all[key]), { status: 200 });
  }) as unknown as typeof fetch;
  return { fetchFn, requests };
}
