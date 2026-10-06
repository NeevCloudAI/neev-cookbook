"""Turns sandbox audit trails into an evidence pack: CSV records, a per-credential CSV, a Markdown summary, a manifest."""
from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Paths whose reading deserves a second look: dotenv files, SSH keys and the account database.
SENSITIVE = re.compile(r"(^|/)\.env(\.[^/]*)?$|(^|/)\.ssh(/|$)|(^|/)id_(rsa|ecdsa|ed25519)\b|^/etc/(passwd|shadow)$")
DELETE_PROGRAMS = {"rm", "rmdir", "unlink", "shred"}
TERMINAL_TOOLS = {"pty_command", "ssh"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "'")  # ' too, so a prefixed value stays unambiguous
LIST_CAP = 50  # errors and sensitive operations listed in the summary; the CSV always has them all
MANIFEST = "MANIFEST.sha256"
RECORD_FIELDS = ("at", "sandbox", "sandbox_id", "record_id", "credential", "tool", "command", "target", "outcome",
                 "reason_code", "duration_ms", "request_id", "pty_id", "seq", "sensitive")


@dataclass
class Trail:
    """One sandbox's records for the window, with the window the server actually served."""

    name: str
    sandbox_id: str
    window_from: datetime
    window_to: datetime
    truncated: bool
    retention_days: int
    records: list


@dataclass
class Row:
    """One audit record as exported: every field as text, plus the sandbox it came from and its flag."""

    at: datetime
    sandbox: str
    sandbox_id: str
    record_id: str
    credential: str
    tool: str
    command: str
    target: str
    outcome: str
    reason_code: str
    duration_ms: str
    request_id: str
    pty_id: str
    seq: str
    sensitive: str

    @property
    def operation(self) -> str:
        """The tool and, for tools that run a program, that program: what the summary groups by."""
        tool = self.tool or "(unnamed)"
        if self.command:
            return f"{tool} {self.command}"
        return f"{tool} (program not recorded)" if tool in ("exec", "process.start") else tool


@dataclass
class Credential:
    """What one credential did across the pack."""

    credential: str
    records: int = 0
    errors: int = 0
    flagged: int = 0
    sandboxes: list[str] = field(default_factory=list)
    first_at: datetime | None = None
    last_at: datetime | None = None
    operations: Counter = field(default_factory=Counter)
    operation_errors: Counter = field(default_factory=Counter)


def stamp(at: datetime) -> str:
    """Formats a time as RFC 3339 UTC with a Z, keeping microseconds only when present."""
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def count(n: int, noun: str) -> str:
    """Writes a count with its noun, plural unless the count is one."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}{'es' if noun.endswith('x') else 's'}"


def _text(v) -> str:
    """Prints a value, an SDK enum such as Outcome.success as its plain value, and None as empty."""
    return "" if v is None else str(getattr(v, "value", v))


def flag(record) -> str | None:
    """Names why a record is sensitive: a read of a secret-looking path, a delete, or a terminal session."""
    tool, target = record.tool or "", record.target or ""
    program = (record.command or "").rsplit("/", 1)[-1]
    if tool in TERMINAL_TOOLS:
        return "terminal session"
    if "remove" in tool or "delete" in tool or program in DELETE_PROGRAMS:
        return "delete"
    if ("read" in tool or "download" in tool) and SENSITIVE.search(target):
        return "sensitive read"
    return None


def make_rows(trails: list[Trail]) -> list[Row]:
    """Flattens every sandbox's records into one list, oldest first."""
    rows = [Row(at=r.at, sandbox=t.name, sandbox_id=t.sandbox_id, record_id=r.id, credential=r.caller_source or "",
                tool=r.tool or "", command=_text(r.command), target=_text(r.target), outcome=_text(r.outcome),
                reason_code=_text(r.reason_code), duration_ms=_text(r.duration_ms), request_id=_text(r.request_id),
                pty_id=_text(r.pty_id), seq=_text(r.seq), sensitive=flag(r) or "")
            for t in trails for r in t.records]
    return sorted(rows, key=lambda r: (r.at, r.sandbox, r.record_id))


def by_credential(rows: list[Row]) -> list[Credential]:
    """Groups rows per credential: counts, errors, flags, sandboxes touched, first and last activity."""
    creds: dict[str, Credential] = {}
    for r in rows:
        c = creds.setdefault(r.credential, Credential(r.credential))
        c.records += 1
        c.errors += r.outcome != "success"
        c.flagged += bool(r.sensitive)
        if r.sandbox not in c.sandboxes:
            c.sandboxes.append(r.sandbox)
        c.first_at = c.first_at or r.at  # rows are oldest first
        c.last_at = r.at
        c.operations[r.operation] += 1
        c.operation_errors[r.operation] += r.outcome != "success"
    return sorted(creds.values(), key=lambda c: -c.records)


def _csv_cell(value: str) -> str:
    """Prefixes ' to a cell a spreadsheet would run as a formula; targets are file names chosen inside the sandbox.

    A value that already starts with ' is prefixed as well, so removing one leading ' always gives the original.
    """
    return "'" + value if value.startswith(FORMULA_START) else value


def _write_csv(path: Path, header, rows) -> None:
    """Writes a CSV with a header row, neutralising formula-like cells."""
    with path.open("w", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        out.writerow(header)
        out.writerows([_csv_cell(str(v)) for v in row] for row in rows)


def _md(text: str) -> str:
    """Escapes a value for a Markdown table cell or list item, so a crafted file name cannot add lines or columns.

    Values from inside the sandbox also go in a code span (see _code), so links, images and HTML stay inert.
    """
    return CONTROL.sub("?", text).replace("|", "\\|").replace("`", "'") or " "


def _code(text: str) -> str:
    """Renders a value from inside the sandbox as an inline code span; _md has already turned backticks into '."""
    return f"`{_md(text)}`" if text else ""


def _short(credential: str) -> str:
    """The first eight characters of a credential id, enough to tell keys apart in prose."""
    return credential[:8] or "(none)"


def _listing(rows: list[Row], what: str) -> list[str]:
    """Lists rows as Markdown bullets, capped at LIST_CAP with a pointer to the CSV."""
    out = [f"- {stamp(r.at)} `{_md(r.sandbox)}` cred `{_short(r.credential)}`: {_code(r.operation)} {_code(r.target)}".rstrip()
           + (f" ({_md(r.reason_code)})" if r.reason_code and r.reason_code != "ok" else "")
           + (f" **{r.sensitive}**" if r.sensitive else "") for r in rows[:LIST_CAP]]
    if len(rows) > LIST_CAP:
        out.append(f"- ...and {len(rows) - LIST_CAP} more in records.csv")
    return out or [f"No {what} in this window."]


def to_markdown(trails: list[Trail], rows: list[Row], since: datetime, until: datetime, generated_at: datetime) -> str:
    """Renders the summary an auditor reads first: scope, per-credential activity, errors, sensitive operations, limits."""
    creds = by_credential(rows)
    errors = [r for r in rows if r.outcome != "success"]
    flagged = [r for r in rows if r.sensitive]
    retention = ", ".join(sorted({f"{t.retention_days} days" for t in trails}))
    out = ["# Audit evidence pack", "",
           f"Generated {stamp(generated_at)}. Requested window: {stamp(since)} to {stamp(until)} (UTC).", "",
           f"{count(len(rows), 'record')} from {count(len(trails), 'sandbox')} under "
           f"{count(len(creds), 'credential')}: {count(len(errors), 'error')}, "
           f"{count(len(flagged), 'sensitive operation')}.", "",
           "This pack is evidence of the operations recorded in these sandboxes' audit trails, exported from the "
           "NeevCloud API. It does not by itself make anyone compliant with SOC 2, RBI or DPDP requirements; which "
           "evidence a control needs is for you and your auditor to decide.", "",
           "## Sandboxes", "", "| Sandbox | Sandbox id | Records | Window served | Note |", "|---|---|---|---|---|"]
    for t in trails:
        note = f"starts at the {t.retention_days}-day retention limit, later than requested" if t.truncated else " "
        out.append(f"| {_md(t.name)} | `{t.sandbox_id}` | {len(t.records)} | {stamp(t.window_from)} to "
                   f"{stamp(t.window_to)} | {note} |")
    out += ["", "## Credentials", "",
            "Each credential is an API key id. It identifies a key, not a person.", "",
            "| Credential | Sandboxes | Records | Errors | Sensitive | First | Last |", "|---|---|---|---|---|---|---|"]
    out += [f"| `{_md(c.credential) if c.credential else '(none)'}` | {_md(', '.join(c.sandboxes))} | {c.records} "
            f"| {c.errors} | {c.flagged} | {stamp(c.first_at)} | {stamp(c.last_at)} |" for c in creds]
    out += ["", "### Operations per credential", "", "| Credential | Operation | Calls | Errors |", "|---|---|---|---|"]
    out += [f"| `{_short(c.credential)}` | {_code(op)} | {n} | {c.operation_errors[op]} |"
            for c in creds for op, n in c.operations.most_common()]
    out += ["", "## Errors", ""] + _listing(errors, "errors")
    out += ["", "## Sensitive operations", "",
            "Flagged: reads of `.env` files, SSH keys, `/etc/passwd` or `/etc/shadow`; deletes (`fs.remove`, or the "
            "programs `rm`, `rmdir`, `unlink`, `shred`); terminal sessions (`pty_command`, `ssh`).", ""]
    out += _listing(flagged, "sensitive operations")
    out += ["", "## What the trail records, and what it does not", "",
            "- Each record holds when the operation happened (server time), the operation, the program it ran "
            "without its arguments, the path it acted on, whether it succeeded and why not, how long it took, the "
            "credential it ran under and a request id.",
            "- A record has no field for command arguments, file contents, or input sent to a program. A command "
            "such as `ls -la` is recorded as `ls`.",
            "- The outcome says whether the call succeeded, not the program's exit code: a command that ran and "
            "exited non-zero is recorded as `success`.",
            "- Work done over the Sandbox MCP Server is recorded like SDK work, under the key that connected, with "
            "no mark that it came over MCP. Give each agent or tenant its own key to tell them apart.",
            "- Commands run through the MCP `exec` tool are currently recorded without their program name, and a "
            "call the sandbox refuses over MCP currently also adds a record with no operation name, shown here as "
            "`(unnamed)`.",
            "- Lifecycle events (creating, pausing or deleting a sandbox) are not part of this trail.",
            f"- The platform reported a retention of {retention}. A requested start older than that is moved "
            "forward to the oldest retained moment, and the sandbox's note above says so.",
            "- The trail of a deleted sandbox can no longer be read through the API, so export before deleting.",
            "", "## Verifying this pack", "",
            f"`{MANIFEST}` lists the SHA-256 of every other file in the pack. Check it with "
            f"`shasum -a 256 -c {MANIFEST}` (macOS) or `sha256sum -c {MANIFEST}` (Linux). Record the SHA-256 of "
            f"`{MANIFEST}` itself, printed when the pack was made, somewhere the recipient can check it "
            "independently, such as the email or ticket the pack is handed over in: a changed manifest then shows.",
            "", "In the CSV files, a value that begins with `=`, `+`, `-`, `@`, a tab or a carriage return is "
            "prefixed with `'` so a spreadsheet does not run it as a formula, and so is a value that already begins "
            "with `'`: remove one leading `'` to get the recorded value.", ""]
    return "\n".join(out)


def to_terminal(trails: list[Trail], rows: list[Row]) -> str:
    """Renders a short plain-text overview of the pack for the progress log."""
    lines = [f"   {count(len(rows), 'record')} from {count(len(trails), 'sandbox')}:"]
    lines += [f"     {t.name:<28} {count(len(t.records), 'record'):>13}" + (f"  (from the {t.retention_days}-day retention limit)" if t.truncated else "")
              for t in trails]
    lines.append("   Per credential:")
    lines += [f"     {_short(c.credential)}  {count(c.records, 'record'):>13}  {count(c.errors, 'error')}  {c.flagged} sensitive  "
              f"in {', '.join(c.sandboxes)}" for c in by_credential(rows)] or ["     no records in this window"]
    return "\n".join(lines)


def _sha256(path: Path) -> str:
    """Hashes a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pack(out: Path, trails: list[Trail], since: datetime, until: datetime, generated_at: datetime) -> str:
    """Writes the pack's files and manifest into out; returns the manifest's own SHA-256."""
    out.mkdir(parents=True, exist_ok=True)
    rows = make_rows(trails)
    _write_csv(out / "records.csv", RECORD_FIELDS,
               [[stamp(r.at) if f == "at" else getattr(r, f) for f in RECORD_FIELDS] for r in rows])
    _write_csv(out / "credentials.csv", ("credential", "sandboxes", "records", "errors", "sensitive", "first_at", "last_at"),
               [[c.credential, ";".join(c.sandboxes), c.records, c.errors, c.flagged, stamp(c.first_at), stamp(c.last_at)]
                for c in by_credential(rows)])
    (out / "summary.md").write_text(to_markdown(trails, rows, since, until, generated_at), encoding="utf-8")
    files = ("credentials.csv", "records.csv", "summary.md")
    (out / MANIFEST).write_text("".join(f"{_sha256(out / name)}  {name}\n" for name in files), encoding="utf-8")
    return _sha256(out / MANIFEST)


def verify_manifest(out: Path) -> list[str]:
    """Re-hashes every file the manifest names; returns the problems found, empty when the pack is intact."""
    manifest = out / MANIFEST
    if not manifest.is_file():
        return [f"{MANIFEST} is missing"]
    problems, entries = [], 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        entries += 1
        if not (out / name).is_file():
            problems.append(f"{name}: missing")
        elif _sha256(out / name) != digest:
            problems.append(f"{name}: does not match its SHA-256 in {MANIFEST}")
    return problems if entries else [f"{MANIFEST} lists no files"]
