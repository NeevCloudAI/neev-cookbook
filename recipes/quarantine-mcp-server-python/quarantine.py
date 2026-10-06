"""Quarantine an untrusted MCP server: run it in a sandbox with egress denied and watch what it tries.

The script installs the MCP package while only the package index is reachable, cuts off all
internet access, then connects an MCP client to the untrusted server from inside the sandbox and
calls its one tool. It then shows the tool's answer, proves no data left the sandbox by probing two
hosts itself, finds the background process the server left in the process list, and prints the
server's own record of the secrets it read — which the sandbox cannot see, so the denied egress,
not any log, is what keeps the data in.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")  # no model is used, so no model key
PREFIX = "quarantine-"
# The hosts pip needs to install the MCP package: the index and the file host it redirects to.
PYPI_HOSTS = ["pypi.org", "files.pythonhosted.org"]
# Managed Python needs --break-system-packages; --user keeps it out of system dirs. No --quiet: the
# exec stream times out after 60s of silence, and pip's progress lines keep it alive.
PIP_INSTALL = ["python3", "-m", "pip", "install", "--user", "--break-system-packages",
               "--disable-pip-version-check", "--no-warn-script-location", "--root-user-action=ignore",
               "mcp>=2.3,<3"]

# Mirrored from untrusted_server.py: the recipe looks for these effects in the locked-down sandbox.
SECRET_FILES = ("/root/.ssh/id_rsa", "/workspace/.env")
EXFIL_HOST = "example.com"
EXFIL_URL = "https://example.com/collect"
BEACON_MARKER = "QUARANTINE_BEACON_TAG"

OBSERVED = "observed.json"  # the server's own record, written under /workspace and read back here
HARNESS_TIMEOUT_MS = 120_000
PROBE_MAX_TIME = 8  # seconds; a denied host times out, so the probe is bounded
PROBE_PAYLOAD = "quarantine-egress-probe"  # a benign marker, so the probe never sends the dummy secret itself
ALLOWED_PROBE_URL = "https://pypi.org/simple/"  # reachable before lockdown; blocked after proves deny_all took hold

# A dummy key the server will try to steal; the base64 body decodes to a NOT-a-real-key marker. The
# header is assembled from parts so a secret scanner on the public repo does not flag the fixture.
_KEY_BODY = "ZGVtby1rZXktZm9yLXRoZS1xdWFyYW50aW5lLXJlY2lwZS1OT1QtYS1yZWFsLWtleQo="
DUMMY_SSH_KEY = (
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"
    f"{_KEY_BODY}\n{_KEY_BODY}\n"
    "-----END " + "OPENSSH PRIVATE KEY-----\n"
)
DUMMY_ENV = (
    "# Dummy .env for the quarantine demo. These are NOT real credentials.\n"
    "API_TOKEN=demo-0000000000000000-not-a-real-key\n"
    "DATABASE_URL=postgres://demo:demo@localhost:5432/demo\n"
)


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def _upload_stats(stdout: str) -> tuple[int, str]:
    """Parses curl's "%{size_upload} %{http_code}" line into (bytes uploaded, http code); unparsable reads as sent."""
    parts = stdout.split()
    try:
        return int(float(parts[0])), parts[1]
    except (IndexError, ValueError):
        return -1, "?"


def egress_probe(sandbox, url: str, max_time: int = PROBE_MAX_TIME):
    """POSTs a benign marker to `url` from inside the sandbox and returns curl's result; used to test egress."""
    args = ["-sS", "--max-time", str(max_time), "-o", "/dev/null", "-w", "%{size_upload} %{http_code}",
            "-X", "POST", "--data", PROBE_PAYLOAD, url]
    return sandbox.exec("curl", args)


def probe_blocked(probe) -> bool:
    """Blocked means curl failed with zero bytes uploaded and no HTTP response, not merely a slow reply."""
    return probe.exit_code != 0 and _upload_stats(probe.stdout) == (0, "000")


def beacon_lines(ps_stdout: str, marker: str = BEACON_MARKER) -> list[str]:
    """Returns the running process lines that carry the beacon marker."""
    return [line.strip() for line in ps_stdout.splitlines() if marker in line]


def read_observed(sandbox) -> dict | None:
    """Reads the server's own record of its covert actions from the sandbox; None if absent or unreadable."""
    try:
        return json.loads(sandbox.files.read(OBSERVED))
    except Exception:
        return None


def plant_secrets(sandbox) -> None:
    """Writes the dummy secret files the untrusted server will try to steal, straight from code."""
    ssh_key = SECRET_FILES[0]  # an absolute home path, so it goes in through exec rather than the workspace API
    sandbox.exec(["sh", "-c", f"mkdir -p $(dirname {ssh_key}) && chmod 700 $(dirname {ssh_key}) && cat > {ssh_key}"],
                 stdin=DUMMY_SSH_KEY)
    sandbox.files.write(".env", DUMMY_ENV)  # a relative path lands under /workspace, matching SECRET_FILES[1]


def upload_code(sandbox, source_dir: Path) -> None:
    """Uploads the untrusted server and the MCP client harness into the sandbox workspace."""
    for name in ("untrusted_server.py", "harness.py"):
        sandbox.files.write(name, (source_dir / name).read_text(encoding="utf-8"))


def parse_harness(result) -> dict | None:
    """Parses the harness's single JSON line from stdout; None when it printed nothing usable."""
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def _answer_text(answer) -> str:
    """Renders the tool's answer, pulling the summary out of a structured result; empty for no answer."""
    if answer is None:
        return ""
    if isinstance(answer, dict):
        return str(answer.get("summary") or answer)
    return str(answer)


def _outcome(record) -> str:
    """Reads an audit record's outcome as plain text, whether it is an enum or already a string."""
    return getattr(record.outcome, "value", str(record.outcome))


def recent_records(sandbox, since: str, limit: int = 100) -> list:
    """Reads the audit trail from `since`, newest first; empty on any error so it never breaks the run."""
    try:
        return sandbox.audit(from_=since, limit=limit).records
    except Exception:
        return []


def verdict(ok: bool, yes: str, no: str) -> str:
    """Formats one PASS/FAIL verdict line for a suspicious behaviour."""
    return f"   [{'PASS' if ok else 'FAIL'}] {yes if ok else no}"


def run(client, source_dir: Path, log=print) -> int:
    """Creates a sandbox, installs the MCP client, locks egress, runs the untrusted server, and reports."""
    sandbox = None
    try:
        log("1. Creating a sandbox that can reach only the package index...")
        sandbox = client.sandboxes.create({"name": f"{PREFIX}{secrets.token_hex(4)}"}, allow_egress=PYPI_HOSTS)
        sandbox.wait_until_ready(timeout_ms=300_000)
        log("2. Installing the MCP package inside the sandbox...")
        installed = sandbox.exec(PIP_INSTALL, timeout_ms=240_000)
        if installed.exit_code != 0:
            log(f"Failed: installing mcp exited {installed.exit_code}: {installed.stderr.strip()[-300:]}")
            return 1
        log("3. Cutting off all internet access before the untrusted server runs...")
        sandbox.update({"egress": {"mode": "deny_all"}})
        log("4. Planting dummy secrets and uploading the untrusted server...")
        plant_secrets(sandbox)
        upload_code(sandbox, source_dir)
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        log("5. Connecting an MCP client to the untrusted server and calling summarize_text...")
        report = parse_harness(sandbox.exec(["python3", "harness.py"], cwd="/workspace",
                                            timeout_ms=HARNESS_TIMEOUT_MS))
        if report is None or not report.get("ok", False):
            reason = (report or {}).get("error") or _answer_text((report or {}).get("answer")) \
                or "the MCP client printed no result"
            log(f"Failed: the MCP client could not exercise the server: {reason}")
            return 1
        log(f"   Tools offered: {', '.join(report.get('tools') or []) or '(none)'}")
        log(f"   Tool answer: {_answer_text(report.get('answer'))}")
        log("6. Looking at what the server did behind that harmless answer:")
        # Two probes: the exfil host, and a host that WAS reachable before lockdown, so a block proves deny_all.
        exfil = egress_probe(sandbox, EXFIL_URL)
        pypi = egress_probe(sandbox, ALLOWED_PROBE_URL)
        sent, code = _upload_stats(exfil.stdout)
        log(f"   Outbound POST to {EXFIL_HOST}: exit {exfil.exit_code}, {sent} bytes uploaded, http {code}")
        log(f"   Outbound POST to the package index (reachable before lockdown): exit {pypi.exit_code}, "
            f"{_upload_stats(pypi.stdout)[0]} bytes uploaded, http {_upload_stats(pypi.stdout)[1]}")
        observed = read_observed(sandbox) or {}
        exfil_state = observed.get("exfil") or {}
        server_blocked = bool(exfil_state.get("blocked", True))  # only False if the server's own send got through
        if exfil_state.get("error"):
            log(f"   The server's own exfil attempt reported: {exfil_state['error']}")
        reads = observed.get("read") or []
        for entry in reads:
            log(f"   Secret read, by the server's own record: {entry.get('path')} ({entry.get('bytes')} bytes)")
        beacons = beacon_lines(sandbox.exec(["ps", "-eo", "pid,args"]).stdout)
        for line in beacons:
            log(f"   Background process still running: {line}")
        log("7. Audit trail (program and target only; never the arguments or any stolen bytes):")
        for record in recent_records(sandbox, since)[:8]:
            parts = [record.tool, record.command or "-", record.target or ""]
            log(f"   {' '.join(p for p in parts if p)} -> {_outcome(record)}")
        blocked = probe_blocked(exfil) and probe_blocked(pypi) and server_blocked
        read_seen = bool(reads)      # the sandbox cannot see in-process reads; this is the server's own report
        spawn_seen = bool(beacons)   # independently observed in the process list
        log("Verdict:")
        log(verdict(blocked, f"exfiltration was blocked: {EXFIL_HOST} and the package index are both unreachable",
                    "exfiltration was NOT fully blocked"))
        log(verdict(read_seen, "the server's secret reads were seen (from its own record)",
                    "no secret read was reported"))
        log(verdict(spawn_seen, "the background process the server spawned was detected in the process list",
                    "no spawned background process was detected"))
        passed = blocked and read_seen and spawn_seen
        log("The quarantine held: the untrusted server was contained and its moves were seen."
            if passed else "The quarantine did not behave as expected.")
        return 0 if passed else 1
    except KeyboardInterrupt:
        log("Interrupted; cleaning up...")
        return 130
    except Exception as exc:  # e.g. a rejected key: one line instead of a traceback
        log(f"Failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if sandbox is not None:
            try:
                sandbox.delete()
                log("   Sandbox deleted.")
            except Exception as exc:  # never mask the real exit with a cleanup error
                log(f"   Warning: could not delete the sandbox ({type(exc).__name__}); remove it from the console.")


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(client, Path(__file__).parent)


if __name__ == "__main__":
    sys.exit(main())
