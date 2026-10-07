// Builds the review packet: the diff of what the agent changed beside the audit trail of what it did.
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { createTwoFilesPatch } from "diff";

export const MAX_FILE_BYTES = 200_000; // larger files are not shown, so they cannot be approved unseen
export const MAX_ENTRIES = 2000; // a listing this long is too big to review by eye
const IGNORED_DIRS = new Set(["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]);
// Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
export const SENSITIVE = /(^|\/)\.env(\.[^/]*)?$|(^|\/)\.ssh(\/|$)|(^|\/)id_(rsa|ecdsa|ed25519)\b|^\/etc\/(passwd|shadow)$/;
export const DELETE_PROGRAMS = new Set(["rm", "rmdir", "unlink", "shred"]);
// Control characters (keeping \t and \n), C1 controls, and invisible or bidi-reordering characters, which can
// make code read differently from how it runs.
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f-\x9f­؜᠎​-‏‪-‮⁠-⁩﻿]/g;
const NOT_RECORDED = "The trail names each operation, the program and the path it acted on, never arguments or file " +
  "contents. Commands the agent runs through MCP exec currently appear without their program name.";

// AuditRecord is the part of one audit record the packet uses, as the SDK returns it.
export interface AuditRecord {
  at: string; id: string; tool: string; command?: string; target?: string; outcome: string;
  reason_code?: string; caller_source?: string; duration_ms?: number;
}

// Entry is the part of a directory-listing entry the diff uses.
export interface Entry { path: string; name: string; type: "file" | "directory" | "symlink"; size: number; symlinkTarget?: string }

// Change is one file that differs between the original project and the sandbox; old/new are null when absent.
// status skipped means seen but not reviewable, so never exported.
export interface Change { path: string; status: "modified" | "added" | "deleted" | "skipped"; old: string | null; new: string | null; note: string }

// Row is one audit record of the agent's session, ready to print.
export interface Row { offsetS: number; tool: string; group: string; target: string; outcome: string; credential: string; flag: string | null }

// Packet is everything a reviewer sees before deciding.
export interface Packet {
  sandbox: string; model: string; task: string; summary: string; changes: Change[];
  activity: Row[]; agentCalls: number; retentionDays: number;
}

// printable replaces control and hidden characters, so sandbox text cannot drive the terminal or hide code.
export const printable = (text: string) => text.replace(CONTROL, "?");
const hasHidden = (text: string) => new RegExp(CONTROL.source).test(text);

// safePath accepts only plain relative paths: no root, no '..', no empty parts, no control or Windows characters.
export function safePath(path: string): boolean {
  if (!path || path.startsWith("/") || hasHidden(path) || path.includes("\\") || path.includes(":")) return false;
  return path.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

// ignored is true for interpreter and test-runner caches, which are noise in a review.
function ignored(path: string): boolean {
  const parts = path.split("/");
  return path.endsWith(".pyc") || parts.slice(0, -1).some((p) => IGNORED_DIRS.has(p)) || IGNORED_DIRS.has(parts.at(-1)!);
}

const skip = (path: string, note: string): Change => ({ path, status: "skipped", old: null, new: null, note });

// collectChanges compares the original files with the sandbox listing, reading each sandbox file with read(path).
// Symlinks, binaries, oversized files and unsafe paths become skipped changes and are never read or exported.
// A path that was skipped is not also reported as deleted.
export async function collectChanges(
  original: Record<string, string>, entries: Entry[], read: (path: string) => Promise<Uint8Array>, maxBytes = MAX_FILE_BYTES,
): Promise<Change[]> {
  const current = new Map<string, string>();
  const skipped: Change[] = [];
  const utf8 = new TextDecoder("utf-8", { fatal: true });
  for (const e of entries) {
    if (e.type === "directory" || ignored(e.path)) continue;
    if (!safePath(e.path)) skipped.push(skip(e.path, "unsafe path"));
    else if (e.type !== "file") skipped.push(skip(e.path, `${e.type} to ${e.symlinkTarget ?? "?"}`));
    else if (e.size > maxBytes) skipped.push(skip(e.path, `too large to review (${e.size} bytes)`));
    else {
      const data = await read(e.path);
      if (data.length > maxBytes) { skipped.push(skip(e.path, `too large to review (${data.length} bytes)`)); continue; } // it may have grown since it was listed
      try { current.set(e.path, utf8.decode(data)); } catch { skipped.push(skip(e.path, "binary file")); }
    }
  }
  const seen = new Set([...current.keys(), ...skipped.map((c) => c.path)]);
  const changes: Change[] = [];
  for (const [p, text] of current) {
    if (!(p in original)) changes.push({ path: p, status: "added", old: null, new: text, note: "" });
    else if (original[p] !== text) changes.push({ path: p, status: "modified", old: original[p], new: text, note: "" });
  }
  for (const p of Object.keys(original)) if (!seen.has(p)) changes.push({ path: p, status: "deleted", old: original[p], new: null, note: "" });
  return [...changes, ...skipped].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

// unified renders one change as a git-style unified diff, marking a missing final newline as git does.
export function unified(change: Change): string {
  if (change.status === "skipped") return "";
  const patch = createTwoFilesPatch(
    change.old !== null ? `a/${change.path}` : "/dev/null", change.new !== null ? `b/${change.path}` : "/dev/null",
    change.old ?? "", change.new ?? "", undefined, undefined, { context: 3 });
  // Keep the git-style part: from the --- line on, without the trailing tab the library puts after each name.
  const lines = patch.split("\n");
  const from = lines.findIndex((l) => l.startsWith("--- "));
  const body = lines.slice(from).map((l) => (l.startsWith("--- ") || l.startsWith("+++ ") ? l.replace(/\t$/, "") : l)).join("\n");
  return body.endsWith("\n") ? body : `${body}\n`;
}

// flag names why a record deserves a look: a read of a sensitive path, or a delete.
function flag(r: AuditRecord): string | null {
  const tool = r.tool ?? ""; const target = r.target ?? "";
  const program = (r.command ?? "").split("/").at(-1)!;
  if (tool.includes("remove") || tool.includes("delete") || DELETE_PROGRAMS.has(program)) return "delete";
  if ((tool.includes("read") || tool.includes("download")) && SENSITIVE.test(target)) return "sensitive read";
  return null;
}

// group labels a record by tool and, for tools that run a program, by that program.
function group(r: AuditRecord): string {
  const tool = r.tool || "(unnamed)";
  if (r.command) return `${tool} ${r.command}`;
  if (tool === "exec" || tool === "process.start") return `${tool} (program not recorded)`;
  return tool;
}

// makeActivity keeps the records after boundary (the script's setup ends there), oldest first.
export function makeActivity(records: AuditRecord[], boundary: string | null): Row[] {
  const after = boundary === null ? records : records.filter((r) => Date.parse(r.at) > Date.parse(boundary));
  const ordered = [...after].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const t0 = ordered.length ? Date.parse(ordered[0].at) : 0;
  return ordered.map((r) => ({
    offsetS: (Date.parse(r.at) - t0) / 1000,
    tool: printable(r.tool || "(unnamed)"),
    group: printable(group(r)),
    target: printable(r.target ?? ""),
    outcome: r.outcome + (r.reason_code && r.reason_code !== "ok" ? ` (${r.reason_code})` : ""),
    credential: (r.caller_source || "-").slice(0, 8), // enough to tell keys apart without printing the full id
    flag: flag(r),
  }));
}

// counts summarises a change as +added/-removed lines, or the reason it was skipped.
function counts(change: Change): string {
  if (change.status === "skipped") return `not exported: ${change.note}`;
  const diff = unified(change).split("\n").slice(2);
  const hidden = hasHidden(change.new ?? "") ? ", hidden characters shown as ?" : "";
  return `+${diff.filter((l) => l.startsWith("+")).length} -${diff.filter((l) => l.startsWith("-")).length}${hidden}`;
}

// trailGap warns when the trail shows fewer records than the calls the agent sent, so the reviewer knows.
function trailGap(p: Packet): string | null {
  if (p.activity.length >= p.agentCalls) return null;
  return `Warning: the audit trail shows ${p.activity.length} of the agent's ${p.agentCalls} calls; ` +
    "records may still be on their way, or a call was refused before it reached the sandbox.";
}

// keep states retention, and that the trail cannot be read back once the sandbox is gone.
const keep = (p: Packet) => `The platform keeps these records for ${p.retentionDays} days, but they cannot be read ` +
  "once the sandbox is deleted: this packet is your copy.";

const pad = (s: string, n: number) => s.padEnd(n);

// toTerminal renders the packet as plain text: summary, changed files, diff, activity, flags and the gaps.
export function toTerminal(p: Packet): string {
  const flagged = p.activity.filter((r) => r.flag);
  const lines = [`Review packet for ${p.sandbox} (${p.model})`, `Task: ${printable(p.task)}`, `Agent says: ${printable(p.summary)}`, "",
    `Changed files (${p.changes.length}):`,
    ...p.changes.map((c) => `  ${pad(c.status, 9)} ${pad(printable(c.path), 28)} ${printable(counts(c))}`),
    "", "Diff:",
    ...p.changes.filter((c) => c.status !== "skipped").map((c) => printable(unified(c)).replace(/\n+$/, "")),
    "", `What the agent did (${p.activity.length} audit records, oldest first):`,
    ...p.activity.map((r) => `  +${r.offsetS.toFixed(1).padStart(5)}s  ${pad(r.group, 30)} ${pad(r.target, 24)} ${pad(r.outcome, 12)} cred ${r.credential}` +
      (r.flag ? `  !! ${r.flag}` : "")),
    "", flagged.length ? `${flagged.length} flagged:` : "Nothing flagged.",
    ...flagged.map((r) => `  +${r.offsetS.toFixed(1)}s  ${r.flag}: ${r.tool} ${r.target}`.trimEnd())];
  const gap = trailGap(p);
  lines.push("", ...(gap ? [gap] : []), NOT_RECORDED, keep(p));
  return lines.join("\n");
}

// code puts untrusted text in a code span, where Markdown links, images and HTML are inert; table-safe.
const code = (text: string) => (text ? "`" + printable(text).replaceAll("`", "'").replaceAll("\n", " ").replaceAll("|", "\\|") + "`" : " ");

// fenced fences untrusted text with a fence longer than any backtick run inside it.
function fenced(text: string, lang = "text"): string[] {
  const clean = printable(text);
  const fence = "`".repeat(Math.max(3, ...(clean.match(/`+/g) ?? []).map((run) => run.length + 1)));
  return [`${fence}${lang}`, clean.replace(/\n+$/, ""), fence];
}

// toMarkdown renders the packet as Markdown; all sandbox and model text is fenced or in code spans, so it cannot render.
export function toMarkdown(p: Packet): string {
  const flagged = p.activity.filter((r) => r.flag);
  const gap = trailGap(p);
  const out = [`# Review packet for ${code(p.sandbox)}`, "", `Model: ${code(p.model)}`, "",
    "Task:", "", ...fenced(p.task), "", "Agent says:", "", ...fenced(p.summary), "",
    "## Changed files", "", "| Status | Path | Lines |", "|---|---|---|",
    ...p.changes.map((c) => `| ${c.status} | ${code(c.path)} | ${code(counts(c))} |`),
    "", "## Diff", "", ...fenced(p.changes.map(unified).join(""), "diff"), "",
    "## What the agent did", "", ...(gap ? [gap, ""] : []),
    "| Time | Tool / program | Target | Outcome | Credential | Flag |", "|---|---|---|---|---|---|",
    ...p.activity.map((r) => `| +${r.offsetS.toFixed(1)}s | ${code(r.group)} | ${code(r.target)} | ${code(r.outcome)} | ${code(r.credential)} | ${r.flag ?? " "} |`),
    "", "## Flagged", "",
    ...(flagged.length ? flagged.map((r) => `- +${r.offsetS.toFixed(1)}s **${r.flag}**: ${code(r.tool)} ${code(r.target)}`) : ["Nothing flagged."]),
    "", "## Not recorded", "", NOT_RECORDED, keep(p), ""];
  return out.join("\n");
}

// exportChanges writes the added and modified files under outDir, which must not exist yet; returns what it wrote.
// Every destination is checked to resolve inside outDir before the first byte is written.
export function exportChanges(changes: Change[], outDir: string): string[] {
  const root = resolve(outDir);
  const planned: [string, string][] = [];
  for (const c of changes) {
    if (c.status !== "added" && c.status !== "modified") continue;
    const dest = resolve(root, c.path);
    const rel = relative(root, dest);
    if (!safePath(c.path) || rel.startsWith("..") || rel === "" || resolve(rel) === rel) throw new Error(`unsafe path ${JSON.stringify(c.path)}; nothing written`);
    planned.push([dest, c.new!]);
  }
  if (existsSync(root)) throw new Error(`${root} already exists; choose a new folder`);
  mkdirSync(root, { recursive: true });
  for (const [dest, content] of planned) {
    if (!dest.startsWith(root + sep)) throw new Error(`unsafe path ${JSON.stringify(dest)}; nothing written`);
    mkdirSync(dirname(dest), { recursive: true });
    writeFileSync(dest, Buffer.from(content, "utf8")); // byte-exact: no newline translation
  }
  return planned.map(([dest]) => dest);
}
