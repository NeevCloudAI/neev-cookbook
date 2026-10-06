"""Turns a sandbox's audit records into a session timeline, for the terminal and as Markdown."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

# Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
SENSITIVE = re.compile(r"(^|/)\.env(\.[^/]*)?$|(^|/)\.ssh(/|$)|(^|/)id_(rsa|ecdsa|ed25519)\b|^/etc/(passwd|shadow)$")
DELETE_PROGRAMS = {"rm", "rmdir", "unlink", "shred"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
NOT_RECORDED = (
    "Not recorded, by design: command arguments, file contents, and anything typed into a "
    "program (passwords, prompts). A record names the program and the path it acted on, so the "
    "trail can be kept and shared without copying secrets into it."
)


@dataclass
class Row:
    """One audit record, ready to print."""

    phase: str
    offset_s: float
    tool: str
    group: str
    target: str
    outcome: str
    duration: str
    credential: str
    flag: str | None


@dataclass
class Report:
    """A session's timeline, oldest first, with how long the trail is kept."""

    sandbox: str
    rows: list[Row]
    retention_days: int

    @property
    def agent_actions(self) -> int:
        """How many records the agent's session produced."""
        return sum(r.phase == "agent" for r in self.rows)


def _printable(text: str) -> str:
    """Replaces control characters, so a crafted file name cannot forge report lines or terminal output."""
    return CONTROL.sub("?", text)


def _value(v) -> str:
    """Prints an SDK enum such as Outcome.success as its plain value."""
    return str(getattr(v, "value", v) or "")


def flag(record) -> str | None:
    """Names why a record deserves a look: a read of a sensitive path, or a delete."""
    tool, target = record.tool or "", record.target or ""
    program = (record.command or "").rsplit("/", 1)[-1]
    if ("remove" in tool or "delete" in tool) or program in DELETE_PROGRAMS:
        return "delete"
    if ("read" in tool or "download" in tool) and SENSITIVE.search(target):
        return "sensitive read"
    return None


def group_key(record) -> str:
    """Groups records by tool and, for tools that run a program, by that program."""
    tool = record.tool or "(unnamed)"
    if record.command:
        return f"{tool} {record.command}"
    if tool in ("exec", "process.start"):
        return f"{tool} (program not recorded)"
    return tool


def make_report(sandbox: str, records, boundary: datetime | None, retention_days: int) -> Report:
    """Orders records oldest first; those at or before boundary are setup, the rest the agent's."""
    ordered = sorted(records, key=lambda r: r.at)
    t0 = ordered[0].at if ordered else None
    rows = [Row(
        phase="setup" if boundary is not None and r.at <= boundary else "agent",
        offset_s=(r.at - t0).total_seconds(),
        tool=_printable(r.tool or "(unnamed)"),
        group=_printable(group_key(r)),
        target=_printable(r.target or ""),
        outcome=_value(r.outcome) + (f" ({r.reason_code})" if r.reason_code and r.reason_code != "ok" else ""),
        duration="-" if r.duration_ms is None else f"{r.duration_ms} ms",
        credential=r.caller_source or "-",
        flag=flag(r)) for r in ordered]
    return Report(sandbox, rows, retention_days)


def _groups(report: Report) -> list[tuple[str, int, int]]:
    """Counts calls and errors per tool/program group, busiest first."""
    calls = Counter(r.group for r in report.rows)
    errors = Counter(r.group for r in report.rows if not r.outcome.startswith("success"))
    return [(g, n, errors[g]) for g, n in calls.most_common()]


def _flagged(report: Report) -> list[Row]:
    """The rows that carry a flag."""
    return [r for r in report.rows if r.flag]


def to_terminal(report: Report) -> str:
    """Renders the report as plain text: timeline, groups, flags and what is not recorded."""
    lines = [f"Session report for {report.sandbox}: {len(report.rows)} records, "
             f"{report.agent_actions} from the agent", "", "Timeline (oldest first):"]
    for r in report.rows:
        lines.append(f"  +{r.offset_s:5.1f}s  {r.phase:<5}  {r.group:<30} {r.target:<22} {r.outcome:<18} "
                     f"{r.duration:>7}  cred {r.credential[:8]}" + (f"  !! {r.flag}" if r.flag else ""))
    lines += ["", "By tool and program:"]
    lines += [f"  {g:<32} {n:>3} calls  {e} errors" for g, n, e in _groups(report)]
    flagged = _flagged(report)
    lines += ["", f"{len(flagged)} flagged:" if flagged else "Nothing flagged."]
    lines += [f"  +{r.offset_s:.1f}s  {r.flag}: {r.tool} {r.target}".rstrip() for r in flagged]
    lines += ["", NOT_RECORDED, f"The trail is kept for {report.retention_days} days."]
    return "\n".join(lines)


def _cell(text: str) -> str:
    """Escapes a value for a Markdown table cell."""
    return text.replace("|", "\\|").replace("`", "'") or " "


def to_markdown(report: Report) -> str:
    """Renders the report as a Markdown document with the same sections as the terminal view."""
    credentials = ", ".join(f"`{c}`" for c in sorted({r.credential for r in report.rows}))
    out = [f"# What did my agent do? Session report for {report.sandbox}", "",
           f"{len(report.rows)} records, {report.agent_actions} from the agent. Credentials: {credentials or '-'}.",
           f"The trail is kept for {report.retention_days} days.", "", "## Timeline", "",
           "| Time | Phase | Tool / program | Target | Outcome | Duration | Credential | Flag |",
           "|---|---|---|---|---|---|---|---|"]
    out += [f"| +{r.offset_s:.1f}s | {r.phase} | {_cell(r.group)} | {_cell(r.target)} | {r.outcome} | {r.duration} "
            f"| `{r.credential[:8]}` | {r.flag or ' '} |" for r in report.rows]
    out += ["", "## By tool and program", "", "| Tool / program | Calls | Errors |", "|---|---|---|"]
    out += [f"| {_cell(g)} | {n} | {e} |" for g, n, e in _groups(report)]
    flagged = _flagged(report)
    out += ["", "## Flagged", ""]
    out += [f"- +{r.offset_s:.1f}s **{r.flag}**: {_cell(r.tool)} {_cell(r.target)}" for r in flagged] or ["Nothing flagged."]
    out += ["", "## Not recorded", "", NOT_RECORDED, ""]
    return "\n".join(out)
