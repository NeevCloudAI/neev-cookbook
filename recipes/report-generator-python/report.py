"""Report generator: an agent turns a CSV into a PDF report and an Excel workbook inside a NeevCloud sandbox."""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import secrets
import sys
import zipfile
import zlib
from pathlib import Path

from agent import AgentFailed, build_report

REQUIRED_ENV = ("NEEV_API_KEY", "NEEV_ORG_ID", "NEEV_PROJECT_ID", "NEEV_MODEL_API_KEY")
MCP_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
MODEL_BASE_URL = "https://inference.ai.neevcloud.com/v1"
DEFAULT_MODEL = "glm-4-7"
SAMPLE_CSV = str(Path(__file__).parent / "data" / "expenses.csv")
DEFAULT_REQUEST = ("Write the 2025 department expense report: actual spend against budget by department and "
                   "by category, the biggest overruns and when they happened, and the monthly spend trend.")
PDF_NAME, XLSX_NAME = "report.pdf", "report.xlsx"
MAX_REPORT_BYTES = 50 * 1024 * 1024  # files come from code the model wrote, so never download an unbounded one
# The only hosts pip needs: the package index and the file host it redirects to.
PYPI_HOSTS = ["pypi.org", "files.pythonhosted.org"]
# The template's Python is managed by the OS, so pip needs --break-system-packages; --user keeps it out of system dirs.
# No --quiet: the exec stream times out after 60s without output, and pip's progress lines keep it alive.
PIP_INSTALL = ["python3", "-m", "pip", "install", "--user", "--break-system-packages",
               "--disable-pip-version-check", "--no-warn-script-location", "--root-user-action=ignore",
               "pandas", "matplotlib", "openpyxl", "fpdf2>=2.8,<3"]  # the prompt names the fpdf2 2.8 API


class ReportInvalid(Exception):
    """A report file is missing or is not what was asked for; the message is shown to the model."""


def _plural(n: int, word: str) -> str:
    """Formats a count with its noun, e.g. "1 page" or "3 pages"."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def check_pdf(data: bytes) -> str:
    """Checks a PDF without a PDF library: header, end marker, page objects and an embedded image; returns a summary."""
    if not data.startswith(b"%PDF-"):
        raise ReportInvalid(f"{PDF_NAME} is not a PDF")
    if b"%%EOF" not in data[-1024:]:
        raise ReportInvalid(f"{PDF_NAME} is cut short")
    # fpdf2 writes page and image objects uncompressed, so their dictionaries can be counted directly.
    pages = len(re.findall(rb"/Type\s*/Page\b", data))
    # A transparent PNG adds a second image object as its mask; count only the pictures.
    images = len(re.findall(rb"/Subtype\s*/Image\b", data)) - len(re.findall(rb"/SMask\s+\d+\s+\d+\s+R", data))
    if not pages:
        raise ReportInvalid(f"{PDF_NAME} has no pages")
    if not images:
        raise ReportInvalid(f"{PDF_NAME} has no chart image")
    return f"{_plural(pages, 'page')}, {_plural(images, 'image')}"


def check_xlsx(data: bytes) -> str:
    """Checks an XLSX as the zip of XML it is: at least two sheets and at least one formula; returns a summary."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # The sizes in the zip directory bound what reading the entries can expand to.
            if sum(i.file_size for i in z.infolist()) > MAX_REPORT_BYTES:
                raise ReportInvalid(f"{XLSX_NAME} unpacks to more than the size cap")
            workbook = z.read("xl/workbook.xml").decode()
            sheets = [z.read(n).decode() for n in z.namelist() if re.fullmatch(r"xl/worksheets/[^/]+\.xml", n)]
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise ReportInvalid(f"{XLSX_NAME} is not an Excel workbook") from None
    names = re.findall(r'<sheet\b[^>]*\bname="([^"]*)"', workbook)
    if len(names) < 2:
        raise ReportInvalid(f"{XLSX_NAME} needs a data sheet and a summary sheet")
    formulas = sum(len(re.findall(r"<f[ >]", sheet)) for sheet in sheets)
    if not formulas:
        raise ReportInvalid(f"{XLSX_NAME} has no formulas; write the summary totals as formulas")
    return f"sheets {', '.join(names)}; {_plural(formulas, 'formula')}"


def fetch_reports(sandbox) -> dict[str, tuple[bytes, str]]:
    """Downloads both reports and checks them; returns {name: (bytes, summary)} or raises ReportInvalid."""
    from neevai.errors import NotFoundError

    fetched = {}
    for name, check in ((PDF_NAME, check_pdf), (XLSX_NAME, check_xlsx)):
        try:
            entry = sandbox.files.stat(name)
        except NotFoundError:
            raise ReportInvalid(f"{name} is missing from the working directory") from None
        if entry.type != "file":  # a symlink's stat size is the link's, not its target's
            raise ReportInvalid(f"{name} is not a regular file")
        if entry.size > MAX_REPORT_BYTES:
            raise ReportInvalid(f"{name} is larger than {MAX_REPORT_BYTES // 2**20} MB")
        data = sandbox.files.read(name)
        fetched[name] = (data, check(data))
    return fetched


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


async def _build(connect, sandbox, model_client, model: str, request: str, log) -> str:
    """Opens the MCP session for the sandbox and runs the agent loop, gating finish on the downloaded reports."""

    async def check() -> str | None:
        """Returns why the reports are not ready yet, or None; the SDK call runs off the event loop."""
        try:
            await asyncio.to_thread(fetch_reports, sandbox)
        except ReportInvalid as e:
            return str(e)
        return None

    try:
        async with connect(sandbox.name) as session:
            return await build_report(session, model_client, model, request, check, log=log)
    except BaseExceptionGroup as group:  # the client's task group wraps errors; surface the real one
        raise _root_cause(group) from None


def _root_cause(e: BaseException) -> BaseException:
    """Unwraps the single-error exception groups the async MCP client raises, to show the real error."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def run(request: str, csv_path: str, out_dir: Path, client, model_client, model: str, connect, log=print) -> int:
    """Creates a sandbox, installs the toolchain, locks egress, has the agent build both reports, downloads them, always deletes."""
    sandbox = None
    try:
        log("1. Creating a sandbox that can reach only pypi.org and files.pythonhosted.org...")
        sandbox = client.sandboxes.create({"name": f"report-gen-{secrets.token_hex(4)}"}, allow_egress=PYPI_HOSTS)
        sandbox.wait_until_ready(timeout_ms=300_000)
        log("2. Installing pandas, matplotlib, openpyxl and fpdf2...")
        installed = sandbox.exec(PIP_INSTALL, timeout_ms=240_000)
        if installed.exit_code != 0:
            log(f"Failed: pip install exited {installed.exit_code}: {installed.stderr.strip()[-300:]}")
            return 1
        # Lock egress before the data arrives: code the model writes can then reach no host at all.
        sandbox.update({"egress": {"mode": "deny_all"}})
        log("3. Internet access removed. Uploading the data...")
        sandbox.files.upload_file(csv_path, "data.csv")
        log(f"4. Asking {model}: {request}")
        findings = asyncio.run(_build(connect, sandbox, model_client, model, request, log))
        log("5. Downloading and checking the reports...")
        reports = fetch_reports(sandbox)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, (data, summary) in reports.items():
            path = out_dir / name
            path.write_bytes(data)
            log(f"   Saved {path} ({summary}, {max(len(data) // 1024, 1)} KB)")
        log("6. Key findings:")
        log(findings)
        return 0
    except KeyboardInterrupt:
        return 130
    except AgentFailed as e:
        log(f"The agent did not finish the report: {e}")
        return 1
    except ReportInvalid as e:
        log(f"Failed: {e}")
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
    parser.add_argument("request", nargs="?", default=DEFAULT_REQUEST)
    parser.add_argument("--csv", default=SAMPLE_CSV, help="CSV file to report on (default: the bundled sample expenses)")
    parser.add_argument("--out-dir", default=".", help="where to save report.pdf and report.xlsx (default: here)")
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
        return run(args.request, args.csv, Path(args.out_dir), client, model_client,
                   os.environ.get("MODEL", DEFAULT_MODEL), connect)


if __name__ == "__main__":
    sys.exit(main())
