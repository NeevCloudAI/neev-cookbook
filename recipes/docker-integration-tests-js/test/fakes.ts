// test/fakes.ts: in-memory stand-ins for a sandbox's files, exec and background processes.
import { COMPOSE_DOWN, COMPOSE_UP, RUN_TESTS } from "../compose-tests.ts";

export const UP_OUTPUT = [" cache Pulled \n api Pulled \n Container sample-app-cache-1  Creating\n",
  " db Pulled \n Container sample-app-db-1  Healthy\n Container sample-app-cache-1  Hea", "lthy\n",
  " Container sample-app-db-1  Healthy\n Container sample-app-api-1  Healthy\n"];
export const TESTS_PASS = ["bun test v1.4.3\n", "(pass) health check answers [3.93ms]\n(pass) creating a note [8ms]\n", "\n 2 pass\n 0 fail\n"];
export const TESTS_FAIL = ["(pass) health check answers [3.93ms]\n(fail) creating a note [8ms]\n", "\n 1 pass\n 1 fail\n"];

// A command's scripted output chunks and exit code; "hang" never exits.
type Script = Record<string, [string[], number | "hang"]>;

export function script(over: Partial<{ up: Script[string]; tests: Script[string]; down: Script[string] }> = {}): Script {
  return { [COMPOSE_UP]: over.up ?? [UP_OUTPUT, 0], [RUN_TESTS]: over.tests ?? [TESTS_PASS, 0], [COMPOSE_DOWN]: over.down ?? [[], 0] };
}

// fakeSandbox plays back one page of output per command, then its exit.
export function fakeSandbox(s: Script) {
  const sb: any = { name: "compose-tests-1", written: new Map<string, unknown>(), started: [] as [string, string][], killed: [] as string[], deleted: false };
  sb.waitUntilReady = async () => sb;
  sb.exec = async () => ({ stdout: "29.1.3\n2.40.3+ds1-0ubuntu1\n", stderr: "", exitCode: 0 });
  sb.files = { write: async (path: string, data: unknown) => { sb.written.set(path, data); } };
  sb.processes = {
    start: async (argv: string[], o: { cwd: string }) => { sb.started.push([argv[2], o.cwd]); return { id: argv[2] }; },
    logs: async (id: string, o: { cursor?: number }) => {
      const [chunks, exit] = s[id];
      if (exit === "hang") return { entries: [], cursor: 0, state: "running" };
      if (o.cursor === undefined) return { entries: chunks.map((data) => ({ stream: "stderr", data })), cursor: 1, state: "running" };
      return { entries: [], cursor: 1, state: "exited" };
    },
    get: async (id: string) => ({ exitCode: s[id][1] }),
    kill: async (id: string) => { sb.killed.push(id); return true; },
  };
  sb.delete = async () => { sb.deleted = true; };
  return sb;
}

export const neevWith = (sb: any) => {
  const created: any[] = [];
  return { created, sandboxes: { create: async (p: any) => { created.push(p); return sb; } } };
};
