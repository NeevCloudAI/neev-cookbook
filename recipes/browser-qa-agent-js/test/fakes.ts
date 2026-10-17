// test/fakes.ts: in-memory stand-ins for the Neev client and a Playwright sandbox.
import { writeFileSync } from "node:fs";
import type { Report } from "../browser-qa.ts";

type Row = [viewport: string, url: string, status: number, ms: number, errors: string[]];

// report builds a qa.js report from rows.
export function report(rows: Row[], links: Partial<Report["links"]> = {}, screenshots = ["phone.png", "tablet.png", "desktop.png"]): Report {
  const pages = rows.map(([viewport, url, status, ms, errors]) => ({ viewport, url, status, ms, errors }));
  return { url: pages[0]?.url ?? "", pages, screenshots, links: { checked: 3, not_checked: 0, broken: [], ...links } };
}

export const DEMO_REPORT = report([
  ["phone", "http://127.0.0.1:8080/", 200, 300, []],
  ["phone", "http://127.0.0.1:8080/about.html", 200, 310, ["loadTeamPhotos is not defined"]],
  ["tablet", "http://127.0.0.1:8080/", 200, 290, []],
  ["tablet", "http://127.0.0.1:8080/about.html", 200, 295, ["loadTeamPhotos is not defined"]],
  ["desktop", "http://127.0.0.1:8080/", 200, 280, []],
  ["desktop", "http://127.0.0.1:8080/about.html", 200, 285, ["loadTeamPhotos is not defined"]],
], { broken: [{ url: "http://127.0.0.1:8080/menu.html", status: 404 }] });

type Event = { type: "stdout" | "stderr"; data: string } | { type: "exit"; exitCode: number } | Error;

// fakeSandbox records uploads, processes and commands; the QA run replays scripted events and a report.
export function fakeSandbox(o: { qaReport?: Report; qaEvents?: Event[]; siteUp?: boolean; onEvent?: () => void } = {}) {
  const { qaReport = DEMO_REPORT, siteUp = true } = o;
  const qaEvents = o.qaEvents ?? [{ type: "stdout", data: "phone   200   300 ms\n" }, { type: "exit", exitCode: 0 }];
  const sb = {
    deleted: false,
    written: new Map<string, string>(), started: [] as string[][], streamed: [] as string[][], downloaded: [] as string[],
    waitUntilReady: async () => sb,
    files: {
      write: async (path: string, content: string) => { sb.written.set(path, content); },
      readText: async (path: string) => { if (path !== "qa-out/report.json") throw new Error(path); return JSON.stringify(qaReport); },
      downloadFile: async (remote: string, local: string) => { sb.downloaded.push(remote); writeFileSync(local, "x"); },
    },
    processes: { start: async (program: string, opts: { args: string[] }) => { sb.started.push([program, ...opts.args]); } },
    exec: (argv: string[], opts: { stream?: boolean }) => {
      if (!opts.stream) return Promise.resolve({ exitCode: siteUp ? 0 : 1, stdout: "", stderr: "" });
      sb.streamed.push(argv);
      return (async function* () {
        for (const event of qaEvents) {
          o.onEvent?.();
          if (event instanceof Error) throw event;
          yield event;
        }
      })();
    },
    delete: async () => { sb.deleted = true; },
  };
  return sb;
}

export const neevWith = (sb: unknown) => {
  const created: any[] = [];
  return { created, sandboxes: { create: async (p: any) => { created.push(p); return sb; } } };
};
