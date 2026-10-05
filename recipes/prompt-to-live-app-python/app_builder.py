"""Prompt to live app: an agent builds a web app in a NeevCloud sandbox and you get a public URL."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time

from agent import AgentFailed, build_app

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
PORT = 3000


def missing_env(environ) -> list[str]:
    """Returns the required variables that are not set."""
    return [name for name in REQUIRED_ENV if not environ.get(name)]


def mcp_connect(api_key: str):
    """Returns connect(sandbox_name): an MCP session on the sandbox MCP server, bound to that one sandbox."""
    from mcp import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    def connect(sandbox_name: str):
        headers = {"Authorization": f"Bearer {api_key}", "x-sandbox-name": sandbox_name}
        return Client(streamable_http_client(MCP_URL, http_client=create_mcp_http_client(headers=headers)))

    return connect


async def _build(connect, sandbox_name: str, model_client, model: str, request: str, log) -> str:
    """Opens the MCP session for the sandbox and runs the agent loop over it."""
    try:
        async with connect(sandbox_name) as session:
            return await build_app(session, model_client, model, request, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def run(request: str, keep_minutes: float, client, model_client, model: str, connect, log=print, wait=time.sleep) -> int:
    """Creates a sandbox, has the agent build the app over MCP, serves it, and always deletes the sandbox."""
    sandbox = None
    served = False
    try:
        log("1. Creating a sandbox (no internet access)...")
        sandbox = client.sandboxes.create({"name": f"live-app-{secrets.token_hex(4)}", "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log(f"2. Asking {model} to build: {request}")
        summary = asyncio.run(_build(connect, sandbox.name, model_client, model, request, log))
        log(f"3. Agent finished: {summary}")
        log(f"4. Starting the app on port {PORT}...")
        # Bind 0.0.0.0: the preview URL cannot reach a server listening on 127.0.0.1.
        sandbox.processes.start(["python3", "-m", "http.server", str(PORT), "--bind", "0.0.0.0"])
        url = sandbox.get_url(PORT)
        served = True
        log(f"5. Live at: {url}")
        if keep_minutes > 0:
            log(f"   Keeping it up for {keep_minutes:g} minutes. Press Ctrl+C to stop sooner.")
            wait(keep_minutes * 60)
        return 0
    except KeyboardInterrupt:
        return 0 if served else 130
    except AgentFailed as e:
        log(f"The agent did not finish the app: {e}")
        return 1
    except Exception as e:  # e.g. a rejected model key: one line instead of a traceback
        cause = _root_cause(e)
        log(f"Failed: {type(cause).__name__}: {cause}")
        return 1
    finally:
        if sandbox is not None:
            sandbox.delete()
            log("   Sandbox deleted.")


def main(argv=None) -> int:
    """Parses arguments, checks the environment, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?", default="a todo app with a dark theme")
    parser.add_argument("--keep", type=float, default=10, help="minutes to keep the app online (default 10)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(args.request, args.keep, client, model_client, os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
