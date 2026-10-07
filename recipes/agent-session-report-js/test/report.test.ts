import assert from "node:assert/strict";
import { test } from "node:test";
import { flag, groupKey, makeReport, toMarkdown, toTerminal } from "../report.ts";
import { CREDENTIAL, FakeSandbox } from "./fakes.ts";

// records builds audit records, oldest first, from [tool, target, command] triples.
function records(...specs: [string, string?, string?][]) {
  const sb = new FakeSandbox();
  for (const [tool, target, command] of specs) sb.record(tool, { target, command });
  return sb.trail;
}

test("sensitive reads and deletes are flagged, nothing else", () => {
  const cases: [string, string | undefined, string | undefined, string | null][] = [
    ["fs.read", ".env", undefined, "sensitive read"],
    ["fs.read", "app/.env.production", undefined, "sensitive read"],
    ["fs.read", "/root/.ssh/id_rsa", undefined, "sensitive read"],
    ["fs.read", "/etc/passwd", undefined, "sensitive read"],
    ["fs.download", "keys/id_ed25519", undefined, "sensitive read"],
    ["fs.read", "settings.ini", undefined, null],
    ["fs.read", "docs/.envelope.md", undefined, null],
    ["fs.write", ".env", undefined, null],
    ["fs.list", ".ssh", undefined, null],
    ["fs.remove", "cache/stale.lock", undefined, "delete"],
    ["exec", undefined, "rm", "delete"],
    ["exec", undefined, "/usr/bin/rm", "delete"],
    ["process.start", undefined, "shred", "delete"],
    ["exec", undefined, "cat", null],
    ["exec", undefined, undefined, null],
  ];
  for (const [tool, target, command, want] of cases) assert.equal(flag(records([tool, target, command])[0]), want, `${tool} ${target ?? command}`);
});

test("records group by tool and program, naming a missing program", () => {
  const [fsRead, execCat, execUnknown] = records(["fs.read", "a"], ["exec", undefined, "cat"], ["exec"]);
  assert.equal(groupKey(fsRead), "fs.read");
  assert.equal(groupKey(execCat), "exec cat");
  assert.equal(groupKey(execUnknown), "exec (program not recorded)");
});

test("the report orders oldest first and splits setup from the agent", () => {
  const trail = records(["fs.write", ".env"], ["fs.read", ".env"], ["exec"]);
  const report = makeReport("session-report-1", [...trail].reverse(), trail[0].at, 30);
  assert.deepEqual(report.rows.map((r) => [r.phase, r.tool]), [["setup", "fs.write"], ["agent", "fs.read"], ["agent", "exec"]]);
  assert.deepEqual(report.rows.map((r) => r.offsetS), [0, 1, 2]);
  assert.equal(report.agentActions, 2);
});

test("terminal and Markdown carry outcome, duration, credential and flags", () => {
  const sb = new FakeSandbox();
  sb.record("fs.write", { target: ".env" });
  sb.record("fs.read", { target: ".env", durationMs: 3 });
  sb.record("fs.read", { target: "missing.txt", outcome: "error", reason: "not_found", durationMs: null });
  const report = makeReport("session-report-1", [...sb.trail].reverse(), sb.trail[0].at, 30);
  for (const out of [toTerminal(report), toMarkdown(report)]) {
    for (const want of ["session-report-1", "sensitive read", "error (not_found)", "3 ms", CREDENTIAL.slice(0, 8), "30 days", "Not recorded"]) {
      assert.ok(out.includes(want), `${want} missing from:\n${out}`);
    }
  }
  const md = toMarkdown(report);
  assert.ok(md.startsWith("# "));
  assert.ok(md.includes("| fs.read | 2 | 1 |")); // by-tool table: calls and errors
  assert.ok(md.includes(`\`${CREDENTIAL}\``));
});

test("control characters in a target cannot forge report lines", () => {
  const report = makeReport("s", records(["fs.read", "x\n| +1.0s | agent | fake |\x1b[2J‮"]), null, 30);
  const md = toMarkdown(report);
  const term = toTerminal(report);
  assert.doesNotMatch(md + term, /[\x1b‮]/);
  assert.equal(md.split("\n").filter((l) => l.startsWith("| +")).length, 1);
});

test("Markdown escapes pipes and backticks in targets", () => {
  const md = toMarkdown(makeReport("s", records(["fs.read", "a|b`.txt"]), null, 30));
  assert.ok(md.includes("a\\|b'.txt"));
});

test("flagged actions are summarised, and a clean session says so", () => {
  const flagged = makeReport("s", [...records(["fs.read", ".env"], ["fs.remove", "x"])].reverse(), null, 30);
  assert.match(toTerminal(flagged), /2 flagged/);
  const clean = makeReport("s", records(["fs.list", "."]), null, 30);
  assert.match(toTerminal(clean), /Nothing flagged/);
  assert.match(toMarkdown(clean), /Nothing flagged/);
});
