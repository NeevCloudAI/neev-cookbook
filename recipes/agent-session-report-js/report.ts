// Turns a sandbox's audit events into a session timeline, for the terminal and as Markdown.
import type { AuditEvent } from "@neevcloud/sdk";

export type { AuditEvent };

// Operations whose process resource is the program they ran, not a process they named.
const PROGRAM_OPS = new Set(["exec", "process.start", "pty_command", "ssh"]);

// Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
export const SENSITIVE = /(^|\/)\.env(\.[^/]*)?$|(^|\/)\.ssh(\/|$)|(^|\/)id_(rsa|ecdsa|ed25519)\b|^\/etc\/(passwd|shadow)$/;
export const DELETE_PROGRAMS = new Set(["rm", "rmdir", "unlink", "shred"]);
// CONTROL matches characters that can drive a terminal or reorder text: C0/C1 controls, bidi and zero-width marks.
const CONTROL = /[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]/g;
const NOT_RECORDED =
  "Not recorded, by design: command arguments, file contents, and anything typed into a " +
  "program (passwords, prompts). An event names the program and the path it acted on, so the " +
  "trail can be kept and shared without copying secrets into it.";

// Row is one audit event, ready to print.
export interface Row {
  phase: "setup" | "agent"; offsetS: number; tool: string; group: string; target: string;
  outcome: string; duration: string; actor: string; flag: string | null;
}

// Report is a session's timeline, oldest first, with how long the trail is kept.
export interface Report { sandbox: string; rows: Row[]; retentionDays: number; agentActions: number }

// printable replaces control characters, so a crafted file name cannot forge report lines or terminal output.
export const printable = (text: string) => text.replace(CONTROL, "?");

// operation is the call an event records, such as fs.read or exec.
export const operation = (event: AuditEvent) => event.api.operation ?? "";

const names = (event: AuditEvent, ...kinds: string[]) =>
  (event.resources ?? []).filter((r) => kinds.includes(r.type) && r.name).map((r) => r.name!);

// program is what an exec, process start or terminal command ran, without its arguments.
export function program(event: AuditEvent): string | undefined {
  if (!PROGRAM_OPS.has(operation(event))) return undefined;
  return names(event, "process", "channel").at(-1);
}

// target is the path an event acted on, or for a process call the process it named.
export function target(event: AuditEvent): string {
  const files = names(event, "file");
  if (files.length) return files[0];
  return PROGRAM_OPS.has(operation(event)) ? "" : (names(event, "process")[0] ?? "");
}

// flag names why an event deserves a look: a read of a sensitive path, or a delete.
export function flag(event: AuditEvent): string | null {
  const tool = operation(event);
  const path = target(event);
  const prog = (program(event) ?? "").split("/").at(-1)!;
  if (tool.includes("remove") || tool.includes("delete") || DELETE_PROGRAMS.has(prog)) return "delete";
  if ((tool.includes("read") || tool.includes("download")) && SENSITIVE.test(path)) return "sensitive read";
  return null;
}

// groupKey groups events by operation and, for operations that run a program, by that program.
export function groupKey(event: AuditEvent): string {
  const tool = operation(event) || "(unnamed)";
  const prog = program(event);
  if (prog) return `${tool} ${prog}`;
  if (tool === "exec" || tool === "process.start") return `${tool} (program not recorded)`;
  return tool;
}

const outcome = (e: AuditEvent) =>
  e.status.toLowerCase() + (e.status_code && e.status_code !== "ok" ? ` (${e.status_code})` : "");

// makeReport orders events oldest first; those at or before boundary (ms since the epoch) are setup, the rest the agent's.
export function makeReport(sandbox: string, events: AuditEvent[], boundary: number | null, retentionDays: number): Report {
  const ordered = [...events].sort((a, b) => a.time - b.time);
  const t0 = ordered.length ? ordered[0].time : 0;
  const rows: Row[] = ordered.map((e) => ({
    phase: boundary !== null && e.time <= boundary ? "setup" : "agent",
    offsetS: (e.time - t0) / 1000,
    tool: printable(operation(e) || "(unnamed)"),
    group: printable(groupKey(e)),
    target: printable(target(e)),
    outcome: printable(outcome(e)),
    duration: e.duration === undefined || e.duration === null ? "-" : `${e.duration} ms`,
    actor: printable(e.actor.user.name || e.actor.user.uid || "-"),
    flag: flag(e),
  }));
  return { sandbox, rows, retentionDays, agentActions: rows.filter((r) => r.phase === "agent").length };
}

// groups counts calls and errors per tool/program group, busiest first.
function groups(report: Report): [string, number, number][] {
  const counts = new Map<string, [number, number]>();
  for (const r of report.rows) {
    const [calls, errors] = counts.get(r.group) ?? [0, 0];
    counts.set(r.group, [calls + 1, errors + (r.outcome.startsWith("success") ? 0 : 1)]);
  }
  return [...counts].map(([g, [n, e]]): [string, number, number] => [g, n, e]).sort((a, b) => b[1] - a[1]);
}

// toTerminal renders the report as plain text: timeline, groups, flags and what is not recorded.
export function toTerminal(report: Report): string {
  const lines = [`Session report for ${report.sandbox}: ${report.rows.length} events, ${report.agentActions} from the agent`, "", "Timeline (oldest first):"];
  for (const r of report.rows) {
    lines.push(`  +${r.offsetS.toFixed(1).padStart(5)}s  ${r.phase.padEnd(5)}  ${r.group.padEnd(30)} ${r.target.padEnd(22)} ${r.outcome.padEnd(18)} ` +
      `${r.duration.padStart(7)}  by ${r.actor.slice(0, 16)}` + (r.flag ? `  !! ${r.flag}` : ""));
  }
  lines.push("", "By tool and program:");
  for (const [g, n, e] of groups(report)) lines.push(`  ${g.padEnd(32)} ${String(n).padStart(3)} calls  ${e} errors`);
  const flagged = report.rows.filter((r) => r.flag);
  lines.push("", flagged.length ? `${flagged.length} flagged:` : "Nothing flagged.");
  for (const r of flagged) lines.push(`  +${r.offsetS.toFixed(1)}s  ${r.flag}: ${r.tool} ${r.target}`.trimEnd());
  lines.push("", NOT_RECORDED, `The trail is kept for ${report.retentionDays} days.`);
  return lines.join("\n");
}

// cell escapes a value for a Markdown table cell.
const cell = (text: string) => text.replaceAll("|", "\\|").replaceAll("`", "'") || " ";

// toMarkdown renders the report as a Markdown document with the same sections as the terminal view.
export function toMarkdown(report: Report): string {
  const actors = [...new Set(report.rows.map((r) => r.actor))].sort().map((a) => `\`${cell(a)}\``).join(", ");
  const out = [`# What did my agent do? Session report for ${report.sandbox}`, "",
    `${report.rows.length} events, ${report.agentActions} from the agent. Made by: ${actors || "-"}.`,
    `The trail is kept for ${report.retentionDays} days.`, "", "## Timeline", "",
    "| Time | Phase | Tool / program | Target | Outcome | Duration | By | Flag |",
    "|---|---|---|---|---|---|---|---|"];
  for (const r of report.rows) {
    out.push(`| +${r.offsetS.toFixed(1)}s | ${r.phase} | ${cell(r.group)} | ${cell(r.target)} | ${cell(r.outcome)} | ${r.duration} ` +
      `| \`${cell(r.actor)}\` | ${r.flag ?? " "} |`);
  }
  out.push("", "## By tool and program", "", "| Tool / program | Calls | Errors |", "|---|---|---|");
  for (const [g, n, e] of groups(report)) out.push(`| ${cell(g)} | ${n} | ${e} |`);
  const flagged = report.rows.filter((r) => r.flag);
  out.push("", "## Flagged", "");
  if (flagged.length) for (const r of flagged) out.push(`- +${r.offsetS.toFixed(1)}s **${r.flag}**: ${cell(r.tool)} ${cell(r.target)}`);
  else out.push("Nothing flagged.");
  out.push("", "## Not recorded", "", NOT_RECORDED, "");
  return out.join("\n");
}
