"""AI data analyst: an agent answers a question about a CSV by running pandas in a NeevCloud Code Interpreter sandbox, and saves a chart."""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from pathlib import Path

from agent import CHART, AgentFailed, analyse

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
SAMPLE_CSV = str(Path(__file__).parent / "data" / "sales.csv")
DEFAULT_QUESTION = ("Which cities and product categories bring in the most revenue, and how much do "
                    "October and November lift sales? Chart monthly revenue for the top cities.")
# Stateful Python kernels with numpy, pandas and matplotlib preinstalled, so nothing is installed at run time.
TEMPLATE = "sb-ubuntu-26-04-interpreter"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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


async def _analyse(connect, sandbox_name: str, model_client, model: str, question: str, log) -> str:
    """Opens the MCP session for the sandbox and runs the agent loop over it."""
    try:
        async with connect(sandbox_name) as session:
            return await analyse(session, model_client, model, question, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def run(question: str, csv_path: str, out_path: Path, client, model_client, model: str, connect, log=print) -> int:
    """Creates a Code Interpreter sandbox, has the agent analyse the CSV over MCP, downloads the chart, always deletes."""
    sandbox = None
    try:
        log("1. Creating a Code Interpreter sandbox with no internet access...")
        started = time.monotonic()
        # No egress at all from the start: code the model writes can reach no host.
        sandbox = client.sandboxes.create({"name": f"data-analyst-{secrets.token_hex(4)}", "sandbox_template_id": TEMPLATE,
                                           "egress": {"mode": "deny_all"}})
        sandbox.wait_until_ready(timeout_ms=300_000)
        log(f"   Ready in {time.monotonic() - started:.1f}s. Uploading the data...")
        sandbox.files.upload_file(csv_path, "data.csv")
        log(f"2. Asking {model}: {question}")
        started = time.monotonic()
        findings = asyncio.run(_analyse(connect, sandbox.name, model_client, model, question, log))
        log(f"   Analysis took {time.monotonic() - started:.1f}s")
        log(f"3. Downloading {CHART}...")
        chart = sandbox.files.read(CHART)
        if not chart.startswith(PNG_SIGNATURE):
            log(f"Failed: the agent's {CHART} is not a PNG image")
            return 1
        out_path.write_bytes(chart)
        log(f"   Saved {out_path} ({len(chart) // 1024} KB)")
        log("4. Findings:")
        log(findings)
        return 0
    except KeyboardInterrupt:
        return 130
    except AgentFailed as e:
        log(f"The agent did not finish the analysis: {e}")
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
    """Parses arguments, checks the environment and the CSV, and runs the recipe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument("--csv", default=SAMPLE_CSV, help="CSV file to analyse (default: the bundled sample sales data)")
    parser.add_argument("--out", default="chart.png", help="where to save the chart (default: chart.png)")
    args = parser.parse_args(argv)
    missing = missing_env(os.environ)
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    if not os.path.isfile(args.csv):
        print(f"CSV file not found: {args.csv}", file=sys.stderr)
        return 2
    from neevai import NeevAI
    from openai import AsyncOpenAI

    model_client = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key=os.environ["NEEV_MODEL_API_KEY"])
    connect = mcp_connect(os.environ["NEEV_API_KEY"])
    with NeevAI() as client:
        return run(args.question, args.csv, Path(args.out), client, model_client,
                   os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
