"""Run Playwright inside a NeevCloud sandbox and pull the screenshot back.

Also shows the egress allow-list. Egress denies everything by default, so the
sandbox reaches only the hosts named here -- the package indexes it needs to
install, and the one site it is allowed to visit. Nothing else.
"""

import os
import uuid

from neevai import NeevAI

TARGET = os.environ.get("TARGET_URL", "https://example.com")

# Everything the sandbox is allowed to reach. Drop a host and the step that
# needs it fails, which is the point.
ALLOWED = [
    {"host": "pypi.org"},
    {"host": "files.pythonhosted.org"},
    {"host": "cdn.playwright.dev"},
    # playwright install --with-deps shells out to apt.
    {"host": "archive.ubuntu.com"},
    {"host": "security.ubuntu.com"},
    {"host": "ports.ubuntu.com"},
    {"host": "example.com"},
]

SCRIPT = """
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto("TARGET_URL", wait_until="networkidle")
    page.screenshot(path="shot.png")
    print(page.title())
    browser.close()
"""

client = NeevAI()


def main() -> None:
    """Install a browser in the sandbox, screenshot a page, save it locally."""
    sandbox = client.sandboxes.create(
        {
            "name": f"pw-{uuid.uuid4().hex[:8]}",
            "egress": {"mode": "allow_list", "allow": ALLOWED},
        }
    )
    sandbox.wait_until_ready()
    print(f"  sandbox ready: {sandbox.name}")

    try:
        print("  installing playwright (a few minutes on first run)")
        install = sandbox.exec(
            "sh",
            args=[
                "-lc",
                "apt-get update -qq && pip install -q playwright "
                "&& playwright install --with-deps chromium",
            ],
            timeout_ms=900_000,
        )
        if install.exit_code != 0:
            print(f"  install failed: {(install.stderr or install.stdout)[-400:]}")
            return

        sandbox.files.write("shot.py", SCRIPT.replace("TARGET_URL", TARGET))
        run = sandbox.exec("python3", args=["shot.py"], timeout_ms=180_000)
        if run.exit_code != 0:
            print(f"  run failed: {(run.stderr or run.stdout)[-400:]}")
            return
        print(f"  page title: {run.stdout.strip()}")

        png = sandbox.files.read("shot.png")
        with open("screenshot.png", "wb") as f:
            f.write(png)
        print(f"  saved screenshot.png ({len(png)} bytes)")

        # The allow-list is real: a host that is not on it cannot be reached.
        blocked = sandbox.exec(
            "sh",
            args=["-lc", "curl -s -m 10 -o /dev/null -w '%{http_code}' https://wikipedia.org || echo blocked"],
            timeout_ms=30_000,
        )
        print(f"  reaching a host not on the allow-list: {blocked.stdout.strip() or 'blocked'}")
    finally:
        sandbox.delete()
        print("\n  sandbox deleted")


if __name__ == "__main__":
    main()
