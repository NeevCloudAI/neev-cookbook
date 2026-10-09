"""Deletes sandboxes the nightly recipes left behind in the nightly project.

Every recipe deletes its own sandboxes, so anything found here means a run was
killed outright. Only names with a recipe prefix are touched.
"""
import sys

from neevai import NeevAI

PREFIXES = ("live-app-", "coding-agent-", "session-report-", "injection-proof-",
            "undo-mistake-", "best-of-n-", "data-analyst-", "crew-", "mcp-undo-",
            "safe-install-", "evidence-", "sleepy-agent-", "golden-", "grader-", "code-runner-",
            "review-gate-", "debug-fail-", "egress-approval-", "tutor-", "report-gen-", "fix-test-",
            "code-mode-", "quarantine-", "eval-roll-", "hosted-agent-", "pr-review-",
            # examples; "crew-" above also covers crewai-python and langgraph-python
            "hello-world-", "egress-allow-", "egress-deny-", "lc-", "oa-", "pw-", "ai-sdk-")
PAGE = 100


def leftovers(client) -> list:
    """Lists every sandbox in the project whose name starts with a recipe prefix."""
    found, page = [], 1
    while True:
        items = client.sandboxes.list(page=page, limit=PAGE).items or []
        found += [s for s in items if s.name.startswith(PREFIXES)]
        if len(items) < PAGE:
            return found
        page += 1


def leftover_agents(client) -> list:
    """Lists every hosted agent in the project whose name starts with a recipe prefix."""
    found, page = [], 1
    while True:
        items = client.agents.list(page=page, limit=PAGE).items or []
        found += [a for a in items if a.name.startswith(PREFIXES)]
        if len(items) < PAGE:
            return found
        page += 1


def main() -> int:
    with NeevAI() as client:
        for agent in leftover_agents(client):
            try:
                client.agents.delete(agent.id)
                print(f"deleted leftover agent {agent.name}")
            except Exception as e:  # keep going; report the ones that could not be removed
                print(f"could not delete agent {agent.name}: {type(e).__name__}: {e}")
        stale = leftovers(client)
        for sandbox in stale:
            try:
                client.sandboxes.delete(sandbox.id)
                print(f"deleted leftover {sandbox.name}")
            except Exception as e:  # keep going; report the ones that could not be removed
                print(f"could not delete {sandbox.name}: {type(e).__name__}: {e}")
        print(f"{len(stale)} leftover sandboxes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
