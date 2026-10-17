// test/fakes.ts: in-memory stand-ins for the Neev client and its sandboxes.
import type { NeevLike, SandboxLike } from "../bring-your-own-image.ts";

// tap plays node --test over the bundled tests: the discount test fails while the planted bug is in cart.js.
export function tap(cartJs: string): [number, string] {
  const buggy = cartJs.includes("subtotal - discountPercent");
  const lines = ["TAP version 13", "ok 1 - adds up price times quantity", "  ---", "  duration_ms: 0.8", "  ...",
    "ok 2 - an empty cart costs nothing",
    ...(buggy ? ["not ok 3 - takes 10% off", "  ---", "  expected: 1800", "  actual: 1990", "  ..."] : ["ok 3 - takes 10% off"]), "1..3"];
  return [buggy ? 1 : 0, lines.join("\n") + "\n"];
}

export type FakeSandbox = SandboxLike & { store: Map<string, string>; execs: [string[], string | undefined][]; snapshots: unknown[]; deleted: boolean };

// fakeNeev's lostReply makes the server create the sandbox or fork even though the call then throws.
export function fakeNeev(o: { node?: string | null; createError?: Error; forkError?: Error; lostReply?: boolean } = {}) {
  const node = o.node === undefined ? "v22.23.3" : o.node;
  const made: FakeSandbox[] = [];
  const created: unknown[] = [];
  const sandbox = (name: string, store = new Map<string, string>()): FakeSandbox => {
    const sb: FakeSandbox = {
      name, store, execs: [], snapshots: [], deleted: false,
      async waitUntilReady() { return sb; },
      async exec(command, opts = {}) {
        sb.execs.push([command, opts.cwd]);
        if (command.join(" ") === "node --version") return { exitCode: node === null ? 127 : 0, stdout: node ?? "", stderr: "" };
        if (command[1] === "--test") { const [exitCode, stdout] = tap(store.get(`${opts.cwd}/cart.js`) ?? ""); return { exitCode, stdout, stderr: "" }; }
        throw new Error(`unexpected command ${command.join(" ")}`);
      },
      files: {
        async write(path, content) { store.set(path, content); return { bytesWritten: content.length }; },
        async readText(path) { return store.get(path)!; },
      },
      async snapshot(params) { sb.snapshots.push(params); return { id: "snap-1", status: "Ready" }; },
      async fork(forkName) {
        const forked = sandbox(forkName, new Map(store));
        if (o.forkError && !o.lostReply) throw o.forkError;
        made.push(forked);
        if (o.forkError) throw o.forkError;
        return forked;
      },
      async delete() { sb.deleted = true; },
    };
    return sb;
  };
  const neev: NeevLike = {
    sandboxes: {
      async create(params) {
        created.push(params);
        if (o.createError && !o.lostReply) throw o.createError;
        const sb = sandbox(params.name);
        made.push(sb);
        if (o.createError) throw o.createError;
        return sb;
      },
      async list({ name }) { return { items: made.filter((s) => s.name.startsWith(name)) }; },
    },
  };
  return { neev, made, created };
}
