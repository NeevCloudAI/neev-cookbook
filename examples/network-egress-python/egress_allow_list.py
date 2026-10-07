"""Control what a sandbox can reach on the network.

Egress denies everything by default. You name the hosts the agent actually needs
and nothing else gets out, so code that is talked into exfiltrating data has
nowhere to send it.

This creates two sandboxes -- one locked down, one with nothing allowed -- and
checks both from the inside.
"""

import os
import sys
import uuid

from neevai import NeevAI

ALLOWED_HOST = os.environ.get("ALLOWED_HOST", "example.com")
BLOCKED_HOST = os.environ.get("BLOCKED_HOST", "wikipedia.org")

client = NeevAI()


def can_reach(sandbox, host: str) -> bool:
    """True if the sandbox can open an HTTPS connection to host."""
    result = sandbox.exec(
        "sh",
        args=["-lc", f"curl -s -m 8 -o /dev/null -w '%{{http_code}}' https://{host} || echo 000"],
        timeout_ms=30_000,
    )
    code = result.stdout.strip()[-3:]
    return code.isdigit() and code != "000"


def main() -> int:
    """Compare an allow-listed sandbox against one that may reach nothing; returns 1 if either leaks."""
    suffix = uuid.uuid4().hex[:8]

    allowed = client.sandboxes.create(
        {
            "name": f"egress-allow-{suffix}",
            "egress": {"mode": "allow_list", "allow": [{"host": ALLOWED_HOST}]},
        }
    )
    denied = None
    try:
        # Omitting egress entirely is the secure default: deny everything.
        denied = client.sandboxes.create({"name": f"egress-deny-{suffix}"})
        allowed.wait_until_ready()
        denied.wait_until_ready()
        print("  two sandboxes ready\n")

        # (label, sandbox, host, whether the policy lets it through)
        checks = [
            ("allow-listed sandbox", allowed, ALLOWED_HOST, True),
            ("allow-listed sandbox", allowed, BLOCKED_HOST, False),
            ("default sandbox     ", denied, ALLOWED_HOST, False),
        ]
        results = []
        for label, sandbox, host, expected in checks:
            reached = can_reach(sandbox, host)
            print(f"  {label} -> {host} : {reached}")
            results.append(reached == expected)
    finally:
        allowed.delete()
        if denied is not None:
            denied.delete()
        print("\n  both sandboxes deleted")
    if not all(results):
        print("  UNEXPECTED: a sandbox reached a host its policy blocks, or missed one it allows")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
