"""Live egress approval: an offline agent asks for each host it needs, a human approves, the allow-list updates live."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from agent import run_agent

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
DEFAULT_REQUEST = (
    "Find the latest release tag of the GitHub repository astral-sh/uv using the GitHub API (api.github.com), "
    "and check whether that version is also the latest uv on PyPI (pypi.org). Answer in two sentences."
)

CONTROL_HOST = "example.com"   # never requested, so it must stay blocked: the boundary check that needs no model
MAX_HOST_REQUESTS = 4          # a model asking for host after host is refused rather than prompting forever
PROBE_MAX_TIME = 5             # seconds; a blocked host times out rather than failing fast
REACH_ATTEMPTS = 5
REACH_INTERVAL_S = 1.0
AUDIT_POLL_ATTEMPTS = 6
AUDIT_POLL_INTERVAL_S = 2.0

# One DNS label per part, letters-only TLD: rejects URLs, ports, wildcards, IP addresses and CIDRs like 0.0.0.0/0.
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_UNPRINTABLE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x1f\x7f-\x9f]")


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def normalize_host(raw: str) -> str | None:
    """Returns the lowercase hostname if raw is exactly one, else None; this is all the model can put on the list."""
    host = str(raw).strip().lower()
    return host if _HOST.match(host) else None


def _printable(text: str, limit: int | None = None) -> str:
    """Strips escape sequences and control characters so model text cannot rewrite what the terminal shows."""
    return _UNPRINTABLE.sub("", str(text))[:limit]


def mask_id(value) -> str:
    """Masks a credential id, keeping its shape, since run logs get shared (CI logs are often public)."""
    return re.sub(r"[0-9a-zA-Z]", "x", value) if value else "-"


def probe(sandbox, host: str, max_time: int = PROBE_MAX_TIME):
    """Runs one bounded HTTPS request to host inside the sandbox; returns (exit code, http code)."""
    r = sandbox.exec("curl", ["-sS", "--max-time", str(max_time), "-o", "/dev/null", "-w", "%{http_code}",
                              f"https://{host}/"])
    return r.exit_code, (r.stdout or "").strip() or "000"


def is_blocked(result) -> bool:
    """Blocked means curl failed and no HTTP response came back at all."""
    return result[0] != 0 and result[1] == "000"


class EgressGate:
    """Decides each request_egress call and applies approvals to the running sandbox's allow-list.

    --auto-approve hosts pass without a prompt; with auto_deny every other host is refused without one,
    otherwise a person answers y/N. Each host is decided once.
    """

    def __init__(self, sandbox, auto_approve: set[str], auto_deny: bool, ask=input, log=print,
                 sleep=time.sleep, clock=time.monotonic):
        self.sandbox, self.auto_approve, self.auto_deny = sandbox, auto_approve, auto_deny
        self.ask, self.log, self.sleep, self.clock = ask, log, sleep, clock
        self.decided: dict[str, str] = {}
        self.approved: list[str] = []
        self.denied: list[str] = []
        self.blocked_before: dict[str, bool] = {}
        self.reachable_after_s: dict[str, float | None] = {}
        self.probes = 0

    def __call__(self, raw_host: str, reason: str) -> str:
        """Answers one request; Ctrl+C raises at once here, though asyncio's own handler would only cancel later."""
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            return self._answer(raw_host, reason)
        finally:
            signal.signal(signal.SIGINT, previous)

    def _answer(self, raw_host: str, reason: str) -> str:
        """Returns the text the model sees: approved, denied, or an error."""
        host = normalize_host(raw_host)
        if host is None:
            return (f"error: {_printable(raw_host, 80)!r} is not a hostname; ask for one exact host such as "
                    "api.github.com (no scheme, path, port, wildcard or IP)")
        if host in self.decided:
            return self.decided[host]
        if len(self.decided) >= MAX_HOST_REQUESTS:
            return f"denied: the limit of {MAX_HOST_REQUESTS} host requests is reached; finish with what you have"
        self.log(f"      reason: {_printable(reason, 200)}")
        approve, how = self._decide(host)
        if approve:
            answer = self._approve(host, how)
        else:
            self.denied.append(host)
            self.log(f"      denied ({how}); the allow-list is unchanged")
            answer = (f"denied: a human did not approve {host}. Do not try to reach it; finish the task without it "
                      "and say what you could not do.")
        self.decided[host] = answer
        return answer

    def _decide(self, host: str) -> tuple[bool, str]:
        """Returns (approve, how it was decided) from the flags, or from a person at the terminal."""
        if host in self.auto_approve:
            return True, "--auto-approve"
        if self.auto_deny:
            return False, "--auto-deny"
        try:
            answer = self.ask(f"      Allow this sandbox to reach {host}? [y/N] ")
        except EOFError:  # no terminal to answer from: the safe default
            return False, "no answer"
        return answer.strip().lower() in ("y", "yes"), "by you"

    def _approve(self, host: str, how: str) -> str:
        """Shows the host blocked, adds it to the live allow-list, then times how soon it answers."""
        self.probes += 1
        self.blocked_before[host] = is_blocked(probe(self.sandbox, host))
        try:
            self.sandbox.update({"egress_add": {"allow": [{"host": host}]}})
        except Exception as e:  # the policy did not change, so this is not an approval
            self.log(f"      approved ({how}) but the allow-list update failed: {e}")
            self.denied.append(host)  # still off the list, so the final check must find it blocked
            return f"error: {host} could not be added to the allow-list; finish without it"
        added = self.clock()
        self.reachable_after_s[host] = None
        for attempt in range(REACH_ATTEMPTS):
            self.probes += 1
            if not is_blocked(probe(self.sandbox, host)):
                self.reachable_after_s[host] = self.clock() - added
                break
            if attempt < REACH_ATTEMPTS - 1:
                self.sleep(REACH_INTERVAL_S)
        self.approved.append(host)
        # A later host can already answer before its own approval; report it rather than claim a live change.
        before = ("blocked" if self.blocked_before[host] else
                  "already reachable" if self.approved[:-1]
                  else "ALREADY REACHABLE with an empty allow-list")
        after = (f"reachable {self.reachable_after_s[host]:.1f}s later" if self.reachable_after_s[host] is not None
                 else "STILL NOT REACHABLE")
        self.log(f"      approved ({how}); before: {before}; added to the live allow-list, no restart; {after}")
        if self.reachable_after_s[host] is None:
            return f"approved: {host} is on the allow-list, but it is not answering; finish without it if it stays down"
        return f"approved: {host} is on the allow-list now and reachable"


def exec_records(sandbox, since: str, expected: int, attempts: int = AUDIT_POLL_ATTEMPTS,
                 interval_s: float = AUDIT_POLL_INTERVAL_S, sleep=time.sleep, log=print) -> list:
    """Polls the audit trail from `since` until `expected` exec records appear; returns them oldest first."""
    records = []
    for attempt in range(attempts):
        records = [r for r in sandbox.audit(from_=since, limit=100).records if r.tool == "exec"]
        if len(records) >= expected:
            break
        if attempt < attempts - 1:
            sleep(interval_s)
    else:
        log(f"   ({len(records)} of {expected} exec records in the audit trail so far; it can lag a few seconds)")
    return list(reversed(records))


def _outcome(record) -> str:
    """Reads an audit record's outcome as a plain string, whether it is an enum or already text."""
    return getattr(record.outcome, "value", str(record.outcome))


def _clock_time(at) -> str:
    """Formats an audit timestamp as HH:MM:SS UTC."""
    return at.strftime("%H:%M:%S") if hasattr(at, "strftime") else str(at)[11:19]


def _find(client, name: str):
    """Returns the sandbox with this name, or None if it does not exist."""
    try:
        return client.sandboxes.get(name)
    except Exception:
        return None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


async def _work(connect, sandbox_name: str, model_client, model: str, request: str, gate, log):
    """Opens the MCP session bound to the sandbox and runs the agent loop with the gate answering requests."""
    try:
        async with connect(sandbox_name) as session:
            return await run_agent(session, model_client, model, request, gate, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def run(request: str, client, model_client, model: str, connect, auto_approve: set[str], auto_deny: bool,
        ask=input, log=print, sleep=time.sleep) -> int:
    """Creates an offline sandbox, runs the agent with live approvals, proves the boundary, always deletes it."""
    sandbox, raw_log = None, log

    def log(line: str) -> None:
        """Every line goes through here, so model and sandbox text cannot drive the terminal."""
        raw_log(_printable(line))

    try:
        log("1. Creating a sandbox with an empty egress allow-list (no internet)...")
        name = f"egress-approval-{secrets.token_hex(4)}"
        try:
            sandbox = client.sandboxes.create({"name": name, "egress": {"mode": "allow_list", "allow": []}})
        except KeyboardInterrupt:  # the server may have created it before the reply arrived
            sandbox = _find(client, name)
            raise
        sandbox.wait_until_ready(timeout_ms=300_000)
        # A small margin so local clock skew cannot hide the run's records from the audit window.
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        log(f"2. Asking {model}: {request}")
        gate = EgressGate(sandbox, auto_approve, auto_deny, ask=ask, log=log, sleep=sleep)
        agent = asyncio.run(_work(connect, sandbox.name, model_client, model, request, gate, log))
        log(f"   Agent's answer: {agent.summary}")
        log(f"3. Allow-list now: {', '.join(r['host'] for r in sandbox.refresh().data['egress']['allow'] or []) or '(empty)'}")
        unapproved = [*gate.denied, *([CONTROL_HOST] if CONTROL_HOST not in gate.approved + gate.denied else [])]
        log(f"4. Checking that hosts nobody approved are still blocked: {', '.join(unapproved)}")
        still_blocked = {}
        for host in unapproved:
            result = probe(sandbox, host)
            still_blocked[host] = is_blocked(result)
            log(f"   {host}: curl exit {result[0]}, http {result[1]} ({'blocked' if still_blocked[host] else 'REACHABLE'})")
        log("5. Audit trail of the commands run (UTC time, program, outcome, key; arguments are never recorded):")
        expected = len(agent.exec_commands) + gate.probes + len(unapproved)
        for r in exec_records(sandbox, since, expected, sleep=sleep, log=log):
            log(f"   {_clock_time(r.at)} {r.command or '(program not recorded)'} -> {_outcome(r)}  key {mask_id(r.caller_source)}")
        checks = [("the agent finished its task", agent.finished),
                  ("at least one host was approved", bool(gate.approved))]
        if gate.approved:  # the first approval opens an empty list, so its before/after is the live-change proof
            checks.append((f"{gate.approved[0]} was blocked before its approval", gate.blocked_before[gate.approved[0]]))
        checks += [(f"{host} was reachable after its approval", gate.reachable_after_s[host] is not None)
                   for host in gate.approved]
        checks += [(f"{host} stayed blocked", ok) for host, ok in still_blocked.items()]
        for label, ok in checks:
            log(f"   {'ok  ' if ok else 'FAIL'} {label}")
        if not all(ok for _, ok in checks):
            log("The approval flow did not complete as expected.")
            return 1
        log("Done: approved hosts opened live without a restart, everything else stayed blocked.")
        return 0
    except KeyboardInterrupt:
        raw_log("")  # Ctrl+C at the prompt leaves the cursor on the prompt line
        log("Interrupted.")
        return 130
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        if sandbox is not None:
            sandbox.delete()
            log("   Sandbox deleted.")


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


def main(argv=None) -> int:
    """Parses arguments, checks the environment and the --auto-approve hosts, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?", default=DEFAULT_REQUEST, help="the task for the agent")
    parser.add_argument("--auto-approve", action="append", default=[], metavar="HOST",
                        help="approve this host without asking (repeatable)")
    parser.add_argument("--auto-deny", action="store_true", help="deny every other host without asking")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    bad = [h for h in args.auto_approve if normalize_host(h) is None]
    if bad:
        print(f"--auto-approve takes exact hostnames such as api.github.com, not: {', '.join(bad)}", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    approve = {normalize_host(h) for h in args.auto_approve}
    with NeevAI() as client:
        return run(args.request, client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect,
                   approve, args.auto_deny)


if __name__ == "__main__":
    sys.exit(main())
