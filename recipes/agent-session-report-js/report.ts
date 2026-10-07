// Turns a sandbox's audit records into a session timeline, for the terminal and as Markdown.

// AuditRecord is one record of the sandbox's audit trail, as sandbox.audit() returns it.
export interface AuditRecord {
  at: string; id: string; tool: string; command?: string; target?: string; outcome: string;
  reason_code?: string; caller_source?: string; duration_ms?: number;
}

// Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
export const SENSITIVE = /(^|\/)\.env(\.[^/]*)?$|(^|\/)\.ssh(\/|$)|(^|\/)id_(rsa|ecdsa|ed25519)\b|^\/etc\/(passwd|shadow)$/;
export const DELETE_PROGRAMS = new Set(["rm", "rmdir", "unlink", "shred"]);
// CONTROL matches characters that can drive a terminal or reorder text: C0/C1 controls, bidi and zero-width marks.
const CONTROL = /[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]/g;
const NOT_RECORDED =
  "Not recorded, by design: command arguments, file contents, and anything typed into a " +
  "program (passwords, prompts). A record names the program and the path it acted on, so the " +
  "trail can be kept and shared without copying secrets into it.";

// Row is one audit record, ready to print.
export interface Row {
  phase: "setup" | "agent"; offsetS: number; tool: string; group: string; target: string;
  outcome: string; duration: string; credential: string; flag: string | null;
}

// Report is a session's timeline, oldest first, with how long the trail is kept.
export interface Report { sandbox: string; rows: Row[]; retentionDays: number; agentActions: number }

// printable replaces control characters, so a crafted file name cannot forge report lines or terminal output.
export const printable = (text: string) => text.replace(CONTROL, "?");

// flag names why a record deserves a look: a read of a sensitive path, or a delete.
export function flag(record: AuditRecord): string | null {
  const tool = record.tool ?? "";
  const target = record.target ?? "";
  const program = (record.command ?? "").split("/").at(-1)!;
  if (tool.includes("remove") || tool.includes("delete") || DELETE_PROGRAMS.has(program)) return "delete";
  if ((tool.includes("read") || tool.includes("download")) && SENSITIVE.test(target)) return "sensitive read";
  return null;
}

// groupKey groups records by tool and, for tools that run a program, by that program.
export function groupKey(record: AuditRecord): string {
  const tool = record.tool || "(unnamed)";
  if (record.command) return `${tool} ${record.command}`;
  if (tool === "exec" || tool === "process.start") return `${tool} (program not recorded)`;
  return tool;
}

// makeReport orders records oldest first; those at or before boundary are setup, the rest the agent's.
export function makeReport(sandbox: string, records: AuditRecord[], boundary: string | null, retentionDays: number): Report {
  const ordered = [...records].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const t0 = ordered.length ? Date.parse(ordered[0].at) : 0;
  const rows: Row[] = ordered.map((r) => ({
    phase: boundary !== null && Date.parse(r.at) <= Date.parse(boundary) ? "setup" : "agent",
    offsetS: (Date.parse(r.at) - t0) / 1000,
    tool: printable(r.tool || "(unnamed)"),
    group: printable(groupKey(r)),
    target: printable(r.target ?? ""),
    outcome: printable(r.outcome ?? "") + (r.reason_code && r.reason_code !== "ok" ? ` (${printable(r.reason_code)})` : ""),
    duration: r.duration_ms === undefined || r.duration_ms === null ? "-" : `${r.duration_ms} ms`,
    credential: printable(r.caller_source || "-"),
    flag: flag(r),
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
  const lines = [`Session report for ${report.sandbox}: ${report.rows.length} records, ${report.agentActions} from the agent`, "", "Timeline (oldest first):"];
  for (const r of report.rows) {
    lines.push(`  +${r.offsetS.toFixed(1).padStart(5)}s  ${r.phase.padEnd(5)}  ${r.group.padEnd(30)} ${r.target.padEnd(22)} ${r.outcome.padEnd(18)} ` +
      `${r.duration.padStart(7)}  cred ${r.credential.slice(0, 8)}` + (r.flag ? `  !! ${r.flag}` : ""));
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
  const credentials = [...new Set(report.rows.map((r) => r.credential))].sort().map((c) => `\`${cell(c)}\``).join(", ");
  const out = [`# What did my agent do? Session report for ${report.sandbox}`, "",
    `${report.rows.length} records, ${report.agentActions} from the agent. Credentials: ${credentials || "-"}.`,
    `The trail is kept for ${report.retentionDays} days.`, "", "## Timeline", "",
    "| Time | Phase | Tool / program | Target | Outcome | Duration | Credential | Flag |",
    "|---|---|---|---|---|---|---|---|"];
  for (const r of report.rows) {
    out.push(`| +${r.offsetS.toFixed(1)}s | ${r.phase} | ${cell(r.group)} | ${cell(r.target)} | ${cell(r.outcome)} | ${r.duration} ` +
      `| \`${cell(r.credential.slice(0, 8))}\` | ${r.flag ?? " "} |`);
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
