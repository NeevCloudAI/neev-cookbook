"""Browser QA agent: a real browser QA pass on three viewports, run in an isolated NeevCloud sandbox."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID")
TEMPLATE = "sb-ubuntu-26-04-playwright"
SANDBOX_DIR = Path(__file__).parent / "sandbox"
SITE_FILES = ("index.html", "about.html", "style.css")
DEMO_URL = "http://127.0.0.1:8080/"
REPORTS = ("report.md", "report.json")
VIEWPORTS = (("phone", 8), ("tablet", 10), ("desktop", 10))  # with each summary column's width
# Waits for the demo server, so the first page load never races its startup.
WAIT_FOR_SITE = f"for i in $(seq 50); do curl -sf -o /dev/null {DEMO_URL} && exit 0; sleep 0.2; done; exit 1"


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def egress_for(url: str | None, allow: list[str]) -> dict:
    """No internet for the demo site; for a public URL, only its host and the --allow hosts."""
    if url is None:
        return {"mode": "deny_all"}
    hosts = dict.fromkeys([urlparse(url).hostname, *allow])
    return {"mode": "allow_list", "allow": [{"host": h} for h in hosts]}


def stream(events, log) -> int:
    """Logs the command's output line by line as it arrives and returns its exit code."""
    pending = ""
    for event in events:
        if event["type"] == "exit":
            if pending:
                log(f"   {pending}")
            return event["exit_code"]
        *lines, pending = (pending + event["data"]).split("\n")
        for line in lines:
            log(f"   {line}")
    raise RuntimeError("the QA run ended without an exit code")


def _clip(text: str, limit: int) -> str:
    """Shortens text to fit one terminal column."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def summary(report: dict) -> list[str]:
    """Formats the report as one row per page, with its load time on each viewport, then the broken links."""
    pages: dict[str, dict[str, dict]] = {}
    for visit in report["pages"]:
        url = urlparse(visit["url"])
        pages.setdefault(url.path + (f"?{url.query}" if url.query else ""), {})[visit["viewport"]] = visit
    lines = [f"   {'Page':<16}{'Status':<11}" + "".join(f"{v.capitalize():>{w}}" for v, w in VIEWPORTS) + "  Console errors"]
    for path, visits in pages.items():
        status = "/".join(dict.fromkeys(str(v["status"] or "failed") for v in visits.values()))
        loads = "".join(f"{str(visits[v]['ms']) + ' ms' if v in visits else '-':>{w}}" for v, w in VIEWPORTS)
        errors = list(dict.fromkeys(e for v in visits.values() for e in v["errors"]))
        first = f"  {_clip(errors[0], 60)}" if errors else ""
        lines.append(f"   {_clip(path, 15):<16}{status:<11}{loads}  {len(errors)}{first}")
    links = report["links"]
    lines.append(f"   Broken links: {len(links['broken'])} of {links['checked']} checked, {links['not_checked']} not checked")
    lines += [f"     {b['url']} ({b['status'] or 'unreachable'})" for b in links["broken"]]
    return lines


def run(url: str | None, allow: list[str], out_dir: Path, client, log=print) -> int:
    """Creates a Playwright sandbox, runs the QA pass in it, downloads the results, and always deletes it."""
    sandbox = None
    began = time.monotonic()
    try:
        target, egress = url or DEMO_URL, egress_for(url, allow)
        reach = "internet access to " + ", ".join(h["host"] for h in egress["allow"]) if url else "no internet access"
        log(f"1. Creating a sandbox from {TEMPLATE} ({reach})...")
        sandbox = client.sandboxes.create({"name": f"browser-qa-{secrets.token_hex(4)}", "sandbox_template_id": TEMPLATE, "egress": egress})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log("2. Uploading the QA script...")
        sandbox.files.write("qa.js", (SANDBOX_DIR / "qa.js").read_text())
        if url is None:
            log(f"   Serving the bundled demo site on {DEMO_URL}")
            for name in SITE_FILES:
                sandbox.files.write(f"site/{name}", (SANDBOX_DIR / "site" / name).read_text())
            sandbox.processes.start(["python3", "-m", "http.server", "8080", "--bind", "127.0.0.1", "--directory", "site"])
            if sandbox.exec(["sh", "-c", WAIT_FOR_SITE], timeout_ms=30_000).exit_code != 0:
                raise RuntimeError("the demo site did not start")
        log(f"3. Checking {target} on phone, tablet and desktop...")
        # Streamed, so each page's progress line arrives as it is checked rather than at the end.
        if stream(sandbox.exec_stream(["node", "qa.js", target, *allow], timeout_ms=600_000), log) != 0:
            raise RuntimeError("the QA script failed")
        report = json.loads(sandbox.files.read_text("qa-out/report.json"))
        if not any(p["status"] for p in report["pages"]):
            raise RuntimeError(f"{target} did not load on any viewport")
        log(f"4. Downloading the screenshots and report to {out_dir}/...")
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in (*report["screenshots"], *REPORTS):
            sandbox.files.download_file(f"qa-out/{name}", out_dir / name)
        log("5. Summary:")
        for line in summary(report):
            log(line)
        log(f"   Done in {time.monotonic() - began:.0f} s. Open {out_dir / 'report.md'} and the screenshots.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # one line instead of a traceback
        log(f"Failed: {e}")
        return 1
    finally:
        if sandbox is not None:
            sandbox.delete()
            log("   Sandbox deleted.")


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="a public site to check instead of the bundled demo site")
    parser.add_argument("--allow", action="append", default=[], metavar="HOST",
                        help="another host the site loads assets from (repeatable)")
    parser.add_argument("--out", default="qa-report", help="where to save the results (default qa-report)")
    args = parser.parse_args(argv)
    if args.url is not None and (urlparse(args.url).scheme not in ("http", "https") or not urlparse(args.url).hostname):
        parser.error("--url must start with http:// or https://")
    if args.allow and args.url is None:
        parser.error("--allow needs --url: the demo site runs without internet access")
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI

    with NeevAI() as client:
        return run(args.url, args.allow, Path(args.out), client)


if __name__ == "__main__":
    sys.exit(main())
