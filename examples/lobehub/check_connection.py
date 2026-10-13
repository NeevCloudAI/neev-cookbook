"""Check that a NeevCloud Sandboxes key works over MCP before you paste it into LobeHub.

It makes the same calls LobeHub's connector makes: list the tools, create a sandbox, run a
command in it, and delete it. The sandbox is named lobehub-check-<random>, so it never touches the
one LobeHub opens for you.
"""

import json
import os
import secrets
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp"
TIMEOUT_SECONDS = 60
PREFIX = "lobehub-check-"


class CheckError(Exception):
    pass


def rpc(url: str, key: str, name: str, method: str, params: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Cloudflare in front of the endpoint refuses urllib's default user agent (error 1010).
            "User-Agent": "neev-cookbook-lobehub-check/1.0",
            "x-sandbox-name": name,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        raise CheckError(f"HTTP {error.code}: {error.read().decode().strip()[:200]}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise CheckError(f"cannot reach {url}: {error}") from None

    data = next(
        (line[5:] for line in reversed(text.splitlines()) if line.startswith("data:")),
        text,
    )
    message = json.loads(data)
    if "error" in message:
        raise CheckError(message["error"].get("message", "JSON-RPC error"))
    return message["result"]


def call_tool(url: str, key: str, name: str, tool: str, arguments: dict) -> dict:
    result = rpc(url, key, name, "tools/call", {"name": tool, "arguments": arguments})
    if result.get("isError"):
        text = "".join(part.get("text", "") for part in result.get("content", []))
        raise CheckError(f"{tool}: {text[:200]}")
    return result


def main() -> int:
    key = os.environ.get("NEEV_API_KEY", "")
    if not key:
        print("missing environment variable: NEEV_API_KEY", file=sys.stderr)
        return 2
    url = os.environ.get("NEEV_SANDBOX_MCP_URL", DEFAULT_URL)
    name = PREFIX + secrets.token_hex(4)

    created = False
    try:
        tools = rpc(url, key, name, "tools/list", {})["tools"]
        print(f"  1. {len(tools)} tools published at {url}")

        call_tool(url, key, name, "create_sandbox", {})
        created = True
        print(f"  2. sandbox {name} created")

        output = call_tool(
            url, key, name, "exec", {"program": "sh", "args": ["-c", "echo hello from the sandbox"]}
        )["structuredContent"]
        if output["exit_code"] != 0:
            raise CheckError(f"exec exited {output['exit_code']}: {output['stderr'].strip()}")
        print(f"  3. exec printed: {output['stdout'].strip()}")
    except CheckError as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        if created:
            try:
                call_tool(url, key, name, "delete_sandbox", {})
                print(f"  sandbox {name} deleted")
            except CheckError as error:
                print(f"could not delete {name}: {error}", file=sys.stderr)

    print("\nThe key works. Use the URL and key above in LobeHub.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
