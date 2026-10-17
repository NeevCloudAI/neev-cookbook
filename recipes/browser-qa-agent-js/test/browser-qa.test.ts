import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { egressFor, missingEnv, parseCli, run, SANDBOX_DIR, stream, summary } from "../browser-qa.ts";
import { fakeSandbox, neevWith, report } from "./fakes.ts";

const PY_SANDBOX_DIR = join(SANDBOX_DIR, "..", "..", "browser-qa-agent-python", "sandbox");

async function runWith(sb: ReturnType<typeof fakeSandbox>, o: { url?: string; allow?: string[]; signal?: AbortSignal } = {}) {
  const lines: string[] = [];
  const neev = neevWith(sb);
  const outDir = join(mkdtempSync(join(tmpdir(), "browser-qa-")), "out");
  const code = await run(o.url, o.allow ?? [], outDir, neev, { log: (s) => lines.push(s), signal: o.signal });
  return { code, lines, neev, outDir };
}

test("missingEnv names every missing variable", () => {
  assert.deepEqual(missingEnv({ NEEV_API_KEY: "k" }), ["NEEV_ORG_ID", "NEEV_PROJECT_ID"]);
});

test("the demo site runs with no internet and a public URL allows only its hosts", () => {
  assert.deepEqual(egressFor(undefined, []), { mode: "deny_all" });
  assert.deepEqual(egressFor("https://shop.example.com/cart", ["cdn.example.net", "shop.example.com"]), {
    mode: "allow_list", allow: [{ host: "shop.example.com" }, { host: "cdn.example.net" }] });
});

async function* events(...list: any[]) { yield* list; }

test("stream logs whole lines across chunks and returns the exit code", async () => {
  const lines: string[] = [];
  const code = await stream(events({ type: "stdout", data: "phone 2" }, { type: "stdout", data: "00\ntablet" },
    { type: "stderr", data: " 404" }, { type: "exit", exitCode: 3 }), (s) => lines.push(s));
  assert.equal(code, 3);
  assert.deepEqual(lines, ["   phone 200", "   tablet 404"]);
});

test("stream without an exit event is an error", async () => {
  await assert.rejects(stream(events({ type: "stdout", data: "phone\n" }), () => {}), /exit code/);
});

test("summary has one row per page with each viewport and the broken links", () => {
  const longError = "Failed to load resource: the server responded with a status of 404 (File not found)";
  const lines = summary(report([
    ["phone", "http://h/", 200, 300, []], ["tablet", "http://h/", 200, 290, []], ["desktop", "http://h/", 200, 280, []],
    ["phone", "http://h/menu.html", 404, 10, [longError]], ["tablet", "http://h/menu.html", 404, 11, [longError]],
    ["desktop", "http://h/menu.html", 0, 12, ["Timeout 15000ms exceeded."]],
  ], { broken: [{ url: "http://h/menu.html", status: 404 }, { url: "http://h/gone", status: 0 }], checked: 4, not_checked: 2 }));
  assert.deepEqual(lines[1].trim().split(/\s+/), ["/", "200", "300", "ms", "290", "ms", "280", "ms", "0"]);
  assert.deepEqual(lines[2].trim().split(/\s+/).slice(0, 3), ["/menu.html", "404/failed", "10"]);
  assert.ok(lines[2].includes("  2  Failed to load resource: the server responded with a stat..."));
  assert.deepEqual(lines.slice(3), ["   Broken links: 2 of 4 checked, 2 not checked",
    "     http://h/menu.html (404)", "     http://h/gone (unreachable)"]);
});

test("summary puts each load time under its viewport and keeps query pages apart", () => {
  const lines = summary(report([
    ["phone", "http://h/", 0, 15000, ["Timeout 15000ms exceeded."]], ["tablet", "http://h/", 200, 290, []],
    ["tablet", "http://h/item?id=1", 200, 111, []], ["tablet", "http://h/item?id=2", 200, 112, []],
    ["desktop", "http://h/item?id=1", 200, 221, []],
  ]));
  assert.equal(lines[0], "   Page            Status        Phone    Tablet   Desktop  Console errors");
  assert.equal(lines[1], "   /               failed/200 15000 ms    290 ms         -  1  Timeout 15000ms exceeded.");
  assert.equal(lines[2], "   /item?id=1      200               -    111 ms    221 ms  0");
  assert.equal(lines[3], "   /item?id=2      200               -    112 ms         -  0");
});

test("a screenshot qa.js could not take is not downloaded", async () => {
  const sb = fakeSandbox({ qaReport: report([["phone", "http://127.0.0.1:8080/", 200, 300, []]], {}, ["tablet.png"]) });
  assert.equal((await runWith(sb)).code, 0);
  assert.deepEqual(sb.downloaded, ["qa-out/tablet.png", "qa-out/report.md", "qa-out/report.json"]);
});

test("a demo run serves the bundled site on loopback, runs QA, downloads and deletes", async () => {
  const sb = fakeSandbox();
  const { code, lines, neev, outDir } = await runWith(sb);
  assert.equal(code, 0);
  const params = neev.created[0];
  assert.equal(params.sandbox_template_id, "sb-ubuntu-26-04-playwright");
  assert.deepEqual(params.egress, { mode: "deny_all" });
  assert.ok(params.name.startsWith("browser-qa-"));
  assert.deepEqual([...sb.written.keys()].sort(), ["qa.js", "site/about.html", "site/index.html", "site/style.css"]);
  assert.equal(sb.written.get("qa.js"), readFileSync(join(SANDBOX_DIR, "qa.js"), "utf8"));
  assert.deepEqual(sb.started, [["python3", "-m", "http.server", "8080", "--bind", "127.0.0.1", "--directory", "site"]]);
  assert.deepEqual(sb.streamed, [["node", "qa.js", "http://127.0.0.1:8080/"]]);
  const results = ["phone.png", "tablet.png", "desktop.png", "report.md", "report.json"];
  assert.deepEqual(sb.downloaded, results.map((n) => `qa-out/${n}`));
  assert.deepEqual(readdirSync(outDir).sort(), [...results].sort());
  assert.ok(lines.includes("   phone   200   300 ms"));
  assert.ok(lines.some((l) => l.includes("/about.html") && l.includes("loadTeamPhotos is not defined")));
  assert.ok(sb.deleted);
});

test("a public URL run allows its hosts, uploads no site and checks links on them", async () => {
  const sb = fakeSandbox({ qaReport: report([["phone", "https://example.com/", 200, 900, []]]) });
  const { code, neev } = await runWith(sb, { url: "https://example.com", allow: ["cdn.example.net"] });
  assert.equal(code, 0);
  assert.deepEqual(neev.created[0].egress, { mode: "allow_list", allow: [{ host: "example.com" }, { host: "cdn.example.net" }] });
  assert.deepEqual([...sb.written.keys()], ["qa.js"]);
  assert.deepEqual(sb.started, []);
  assert.deepEqual(sb.streamed, [["node", "qa.js", "https://example.com", "cdn.example.net"]]);
});

for (const [name, sb, message] of [
  ["the demo site does not start", fakeSandbox({ siteUp: false }), "Failed: the demo site did not start"],
  ["the QA script fails", fakeSandbox({ qaEvents: [{ type: "stderr", data: "boom\n" }, { type: "exit", exitCode: 1 }] }), "Failed: the QA script failed"],
  ["nothing loads", fakeSandbox({ qaReport: report([["phone", "http://127.0.0.1:8080/", 0, 15000, ["net::ERR"]]]) }),
    "Failed: http://127.0.0.1:8080/ did not load on any viewport"],
] as const) {
  test(`a failed run (${name}) is one line, exits 1, downloads nothing and deletes`, async () => {
    const { code, lines } = await runWith(sb);
    assert.equal(code, 1);
    assert.ok(lines.includes(message), lines.join("\n"));
    assert.deepEqual(sb.downloaded, []);
    assert.ok(sb.deleted);
  });
}

test("Ctrl+C mid-run deletes the sandbox and exits 130", async () => {
  const ac = new AbortController();
  const sb = fakeSandbox({ qaEvents: [{ type: "stdout", data: "phone\n" }, new Error("aborted")], onEvent: () => ac.abort() });
  const { code } = await runWith(sb, { signal: ac.signal });
  assert.equal(code, 130);
  assert.ok(sb.deleted);
});

test("runs get different sandbox names", async () => {
  const neev = neevWith(fakeSandbox());
  for (let i = 0; i < 2; i++) await run(undefined, [], mkdtempSync(join(tmpdir(), "browser-qa-")), neev, { log: () => {} });
  assert.notEqual(neev.created[0].name, neev.created[1].name);
});

test("parseCli reads --url, repeated --allow and --out, and rejects a bad URL or --allow without --url", () => {
  assert.deepEqual(parseCli([]), { url: undefined, allow: [], out: "qa-report" });
  assert.deepEqual(parseCli(["--url", "https://example.com", "--allow", "a.net", "--allow=b.net", "--out", "x"]),
    { url: "https://example.com", allow: ["a.net", "b.net"], out: "x" });
  assert.throws(() => parseCli(["--url", "example.com"]), /--url/);
  assert.throws(() => parseCli(["--url", "ftp://example.com"]), /--url/);
  assert.throws(() => parseCli(["--allow", "cdn.example.net"]), /--allow needs --url/);
});

test("the Python recipe uploads the same QA script and site", { skip: !existsSync(PY_SANDBOX_DIR) }, () => {
  for (const path of ["qa.js", "site/index.html", "site/about.html", "site/style.css"]) {
    assert.equal(readFileSync(join(SANDBOX_DIR, path), "utf8"), readFileSync(join(PY_SANDBOX_DIR, path), "utf8"), path);
  }
});
