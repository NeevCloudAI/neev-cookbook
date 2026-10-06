"""Builds the review packet: the diff of what the agent changed beside the audit trail of what it did."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 200_000  # larger files are not shown, so they cannot be approved unseen
MAX_ENTRIES = 2000  # a listing this long is too big to review by eye
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
SENSITIVE = re.compile(r"(^|/)\.env(\.[^/]*)?$|(^|/)\.ssh(/|$)|(^|/)id_(rsa|ecdsa|ed25519)\b|^/etc/(passwd|shadow)$")
DELETE_PROGRAMS = {"rm", "rmdir", "unlink", "shred"}
# Control characters (keeping \t and \n), C1 controls, and invisible or bidi-reordering characters, which can
# make code read differently from how it runs.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
NOT_RECORDED = (
    "The trail names each operation, the program and the path it acted on, never arguments or file "
    "contents. Commands the agent runs through MCP exec currently appear without their program name."
)


@dataclass
class Change:
    """One file that differs between the original project and the sandbox; old/new are None when absent."""

    path: str
    status: str  # modified, added, deleted, or skipped (seen but not reviewable, so never exported)
    old: str | None
    new: str | None
    note: str = ""


@dataclass
class Row:
    """One audit record of the agent's session, ready to print."""

    offset_s: float
    tool: str
    group: str
    target: str
    outcome: str
    credential: str
    flag: str | None


@dataclass
class Packet:
    """Everything a reviewer sees before deciding."""

    sandbox: str
    model: str
    task: str
    summary: str
    changes: list[Change]
    activity: list[Row]
    agent_calls: int
    retention_days: int


def printable(text: str) -> str:
    """Replaces control and hidden characters, so sandbox text cannot drive the terminal or hide code."""
    return CONTROL.sub("?", text)


def safe_path(path: str) -> bool:
    """Accepts only plain relative paths: no root, no '..', no empty parts, no control or Windows characters."""
    if not path or path.startswith("/") or CONTROL.search(path) or "\\" in path or ":" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _ignored(path: str) -> bool:
    """True for interpreter and test-runner caches, which are noise in a review."""
    parts = path.split("/")
    return path.endswith(".pyc") or any(p in IGNORED_DIRS for p in parts[:-1]) or parts[-1] in IGNORED_DIRS


def collect_changes(original: dict[str, str], entries, read) -> list[Change]:
    """Compares the original files with the sandbox listing, reading each sandbox file with read(path).

    Symlinks, binaries, oversized files and unsafe paths become 'skipped' changes and are never read
    or exported. A path that was skipped is not also reported as deleted.
    """
    current, skipped = {}, []
    for e in entries:
        if e.type == "directory" or _ignored(e.path):
            continue
        if not safe_path(e.path):
            skipped.append(Change(e.path, "skipped", None, None, "unsafe path"))
        elif e.type != "file":
            skipped.append(Change(e.path, "skipped", None, None, f"{e.type} to {e.symlink_target or '?'}"))
        elif e.size > MAX_FILE_BYTES:
            skipped.append(Change(e.path, "skipped", None, None, f"too large to review ({e.size} bytes)"))
        else:
            data = read(e.path)
            if len(data) > MAX_FILE_BYTES:  # the file may have grown since it was listed
                skipped.append(Change(e.path, "skipped", None, None, f"too large to review ({len(data)} bytes)"))
                continue
            try:
                current[e.path] = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(Change(e.path, "skipped", None, None, "binary file"))
    seen = set(current) | {c.path for c in skipped}
    changes = [Change(p, "modified", original[p], current[p]) for p in current if p in original and original[p] != current[p]]
    changes += [Change(p, "added", None, current[p]) for p in current if p not in original]
    changes += [Change(p, "deleted", original[p], None) for p in original if p not in seen]
    return sorted(changes + skipped, key=lambda c: c.path)


def unified(change: Change) -> str:
    """Renders one change as a git-style unified diff, marking a missing final newline as git does."""
    if change.status == "skipped":
        return ""
    lines = difflib.unified_diff(
        (change.old or "").splitlines(keepends=True), (change.new or "").splitlines(keepends=True),
        fromfile=f"a/{change.path}" if change.old is not None else "/dev/null",
        tofile=f"b/{change.path}" if change.new is not None else "/dev/null")
    return "".join(l if l.endswith("\n") else l + "\n\\ No newline at end of file\n" for l in lines)


def _value(v) -> str:
    """Prints an SDK enum such as Outcome.success as its plain value."""
    return str(getattr(v, "value", v) or "")


def _flag(record) -> str | None:
    """Names why a record deserves a look: a read of a sensitive path, or a delete."""
    tool, target = record.tool or "", record.target or ""
    program = (record.command or "").rsplit("/", 1)[-1]
    if "remove" in tool or "delete" in tool or program in DELETE_PROGRAMS:
        return "delete"
    if ("read" in tool or "download" in tool) and SENSITIVE.search(target):
        return "sensitive read"
    return None


def _group(record) -> str:
    """Labels a record by tool and, for tools that run a program, by that program."""
    tool = record.tool or "(unnamed)"
    if record.command:
        return f"{tool} {record.command}"
    if tool in ("exec", "process.start"):
        return f"{tool} (program not recorded)"
    return tool


def make_activity(records, boundary: datetime | None) -> list[Row]:
    """Keeps the records after boundary (the script's setup ends there), oldest first."""
    ordered = sorted((r for r in records if boundary is None or r.at > boundary), key=lambda r: r.at)
    t0 = ordered[0].at if ordered else None
    return [Row(
        offset_s=(r.at - t0).total_seconds(),
        tool=printable(r.tool or "(unnamed)"),
        group=printable(_group(r)),
        target=printable(r.target or ""),
        outcome=_value(r.outcome) + (f" ({r.reason_code})" if r.reason_code and r.reason_code != "ok" else ""),
        credential=(r.caller_source or "-")[:8],  # enough to tell keys apart without printing the full id
        flag=_flag(r)) for r in ordered]


def _counts(change: Change) -> str:
    """Summarises a change as +added/-removed lines, or the reason it was skipped."""
    if change.status == "skipped":
        return f"not exported: {change.note}"
    diff = unified(change).splitlines()[2:]
    hidden = ", hidden characters shown as ?" if CONTROL.search(change.new or "") else ""
    return f"+{sum(l.startswith('+') for l in diff)} -{sum(l.startswith('-') for l in diff)}{hidden}"


def _trail_gap(packet: Packet) -> str | None:
    """Warns when the trail shows fewer records than the calls the agent sent, so the reviewer knows."""
    if len(packet.activity) >= packet.agent_calls:
        return None
    return (f"Warning: the audit trail shows {len(packet.activity)} of the agent's {packet.agent_calls} calls; "
            "records may still be on their way, or a call was refused before it reached the sandbox.")


def _keep(packet: Packet) -> str:
    """States retention, and that the trail cannot be read back once the sandbox is gone."""
    return (f"The platform keeps these records for {packet.retention_days} days, but they cannot be read "
            "once the sandbox is deleted: this packet is your copy.")


def to_terminal(packet: Packet) -> str:
    """Renders the packet as plain text: summary, changed files, diff, activity, flags and the gaps."""
    flagged = [r for r in packet.activity if r.flag]
    lines = [f"Review packet for {packet.sandbox} ({packet.model})", f"Task: {printable(packet.task)}",
             f"Agent says: {printable(packet.summary)}", "", f"Changed files ({len(packet.changes)}):"]
    lines += [f"  {c.status:<9} {printable(c.path):<28} {printable(_counts(c))}" for c in packet.changes]
    lines += ["", "Diff:"]
    lines += [printable(unified(c)).rstrip("\n") for c in packet.changes if c.status != "skipped"]
    lines += ["", f"What the agent did ({len(packet.activity)} audit records, oldest first):"]
    lines += [f"  +{r.offset_s:5.1f}s  {r.group:<30} {r.target:<24} {r.outcome:<12} cred {r.credential}"
              + (f"  !! {r.flag}" if r.flag else "") for r in packet.activity]
    lines += ["", f"{len(flagged)} flagged:" if flagged else "Nothing flagged."]
    lines += [f"  +{r.offset_s:.1f}s  {r.flag}: {r.tool} {r.target}".rstrip() for r in flagged]
    gap = _trail_gap(packet)
    lines += ["", *([gap] if gap else []), NOT_RECORDED, _keep(packet)]
    return "\n".join(lines)


def _code(text: str) -> str:
    """Puts untrusted text in a code span, where Markdown links, images and HTML are inert; table-safe."""
    return "`" + printable(text).replace("`", "'").replace("\n", " ").replace("|", "\\|") + "`" if text else " "


def _fenced(text: str, lang: str = "text") -> list[str]:
    """Fences untrusted text with a fence longer than any backtick run inside it."""
    text = printable(text)
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", text))])
    return [f"{fence}{lang}", text.rstrip("\n"), fence]


def to_markdown(packet: Packet) -> str:
    """Renders the packet as Markdown; all sandbox and model text is fenced or in code spans, so it cannot render."""
    flagged = [r for r in packet.activity if r.flag]
    gap = _trail_gap(packet)
    out = [f"# Review packet for {_code(packet.sandbox)}", "", f"Model: {_code(packet.model)}", "",
           "Task:", "", *_fenced(packet.task), "", "Agent says:", "", *_fenced(packet.summary), "",
           "## Changed files", "", "| Status | Path | Lines |", "|---|---|---|"]
    out += [f"| {c.status} | {_code(c.path)} | {_code(_counts(c))} |" for c in packet.changes]
    out += ["", "## Diff", "", *_fenced("".join(unified(c) for c in packet.changes), "diff"), "",
            "## What the agent did", "", *([gap, ""] if gap else []),
            "| Time | Tool / program | Target | Outcome | Credential | Flag |", "|---|---|---|---|---|---|"]
    out += [f"| +{r.offset_s:.1f}s | {_code(r.group)} | {_code(r.target)} | {_code(r.outcome)} | {_code(r.credential)} "
            f"| {r.flag or ' '} |" for r in packet.activity]
    out += ["", "## Flagged", ""]
    out += [f"- +{r.offset_s:.1f}s **{r.flag}**: {_code(r.tool)} {_code(r.target)}" for r in flagged] or ["Nothing flagged."]
    out += ["", "## Not recorded", "", NOT_RECORDED, _keep(packet), ""]
    return "\n".join(out)


def export(changes: list[Change], out_dir: Path) -> list[Path]:
    """Writes the added and modified files under out_dir, which must not exist yet; returns what it wrote.

    Every destination is checked to resolve inside out_dir before the first byte is written.
    """
    root = out_dir.resolve()
    planned = []
    for c in changes:
        if c.status not in ("added", "modified"):
            continue
        dest = (root / PurePosixPath(c.path)).resolve()
        if not safe_path(c.path) or not dest.is_relative_to(root):
            raise ValueError(f"unsafe path {c.path!r}; nothing written")
        planned.append((dest, c.new))
    root.mkdir(parents=True, exist_ok=False)
    for dest, content in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content.encode("utf-8"))  # byte-exact: no newline translation
    return [dest for dest, _ in planned]
