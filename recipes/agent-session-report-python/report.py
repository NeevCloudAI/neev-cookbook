"""Turns a sandbox's audit events into a session timeline, for the terminal and as Markdown."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

# Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
SENSITIVE = re.compile(r"(^|/)\.env(\.[^/]*)?$|(^|/)\.ssh(/|$)|(^|/)id_(rsa|ecdsa|ed25519)\b|^/etc/(passwd|shadow)$")
DELETE_PROGRAMS = {"rm", "rmdir", "unlink", "shred"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Operations whose process resource is the program they ran, not a process they named.
PROGRAM_OPS = {"exec", "process.start", "pty_command", "ssh"}
NOT_RECORDED = (
    "Not recorded, by design: command arguments, file contents, and anything typed into a "
    "program (passwords, prompts). An event names the program and the path it acted on, so the "
    "trail can be kept and shared without copying secrets into it."
)


@dataclass
class Row:
    """One audit event, ready to print."""

    phase: str
    offset_s: float
    tool: str
    group: str
    target: str
    outcome: str
    duration: str
    actor: str
    flag: str | None


@dataclass
class Report:
    """A session's timeline, oldest first, with how long the trail is kept."""

    sandbox: str
    rows: list[Row]
    retention_days: int

    @property
    def agent_actions(self) -> int:
        """How many events the agent's session produced."""
        return sum(r.phase == "agent" for r in self.rows)


def _printable(text: str) -> str:
    """Replaces control characters, so a crafted file name cannot forge report lines or terminal output."""
    return CONTROL.sub("?", text)


def _value(v) -> str:
    """Prints an SDK enum such as AuditResourceType.file as its plain value."""
    return str(getattr(v, "value", v) or "")


def event_at(event) -> datetime:
    """When the event was recorded; the trail gives milliseconds since the epoch."""
    return datetime.fromtimestamp(event.time / 1000, tz=timezone.utc)


def operation(event) -> str:
    """The call the event records, such as fs.read or exec."""
    return event.api.operation or ""


def _names(event, *kinds: str) -> list[str]:
    return [r.name for r in event.resources or [] if _value(r.type) in kinds and r.name]


def program(event) -> str | None:
    """The program an exec, process start or terminal command ran, without its arguments."""
    if operation(event) not in PROGRAM_OPS:
        return None
    names = _names(event, "process", "channel")
    return names[-1] if names else None


def target(event) -> str:
    """The path the event acted on, or for a process call the process it named."""
    files = _names(event, "file")
    if files:
        return files[0]
    return "" if operation(event) in PROGRAM_OPS else next(iter(_names(event, "process")), "")


def flag(event) -> str | None:
    """Names why an event deserves a look: a read of a sensitive path, or a delete."""
    tool, path = operation(event), target(event)
    prog = (program(event) or "").rsplit("/", 1)[-1]
    if ("remove" in tool or "delete" in tool) or prog in DELETE_PROGRAMS:
        return "delete"
    if ("read" in tool or "download" in tool) and SENSITIVE.search(path):
        return "sensitive read"
    return None


def group_key(event) -> str:
    """Groups events by operation and, for operations that run a program, by that program."""
    tool = operation(event) or "(unnamed)"
    prog = program(event)
    if prog:
        return f"{tool} {prog}"
    if tool in ("exec", "process.start"):
        return f"{tool} (program not recorded)"
    return tool


def _outcome(event) -> str:
    code = event.status_code
    return event.status.lower() + (f" ({code})" if code and code != "ok" else "")


def _actor(event) -> str:
    user = event.actor.user
    return user.name or user.uid or "-"


def make_report(sandbox: str, events, boundary: datetime | None, retention_days: int) -> Report:
    """Orders events oldest first; those at or before boundary are setup, the rest the agent's."""
    ordered = sorted(events, key=event_at)
    t0 = event_at(ordered[0]) if ordered else None
    rows = [Row(
        phase="setup" if boundary is not None and event_at(e) <= boundary else "agent",
        offset_s=(event_at(e) - t0).total_seconds(),
        tool=_printable(operation(e) or "(unnamed)"),
        group=_printable(group_key(e)),
        target=_printable(target(e)),
        outcome=_outcome(e),
        duration="-" if e.duration is None else f"{e.duration} ms",
        actor=_printable(_actor(e)),
        flag=flag(e)) for e in ordered]
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
    lines = [f"Session report for {report.sandbox}: {len(report.rows)} events, "
             f"{report.agent_actions} from the agent", "", "Timeline (oldest first):"]
    for r in report.rows:
        lines.append(f"  +{r.offset_s:5.1f}s  {r.phase:<5}  {r.group:<30} {r.target:<22} {r.outcome:<18} "
                     f"{r.duration:>7}  by {r.actor[:16]}" + (f"  !! {r.flag}" if r.flag else ""))
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
    actors = ", ".join(f"`{_cell(a)}`" for a in sorted({r.actor for r in report.rows}))
    out = [f"# What did my agent do? Session report for {report.sandbox}", "",
           f"{len(report.rows)} events, {report.agent_actions} from the agent. Made by: {actors or '-'}.",
           f"The trail is kept for {report.retention_days} days.", "", "## Timeline", "",
           "| Time | Phase | Tool / program | Target | Outcome | Duration | By | Flag |",
           "|---|---|---|---|---|---|---|---|"]
    out += [f"| +{r.offset_s:.1f}s | {r.phase} | {_cell(r.group)} | {_cell(r.target)} | {r.outcome} | {r.duration} "
            f"| `{_cell(r.actor)}` | {r.flag or ' '} |" for r in report.rows]
    out += ["", "## By tool and program", "", "| Tool / program | Calls | Errors |", "|---|---|---|"]
    out += [f"| {_cell(g)} | {n} | {e} |" for g, n, e in _groups(report)]
    flagged = _flagged(report)
    out += ["", "## Flagged", ""]
    out += [f"- +{r.offset_s:.1f}s **{r.flag}**: {_cell(r.tool)} {_cell(r.target)}" for r in flagged] or ["Nothing flagged."]
    out += ["", "## Not recorded", "", NOT_RECORDED, ""]
    return "\n".join(out)
