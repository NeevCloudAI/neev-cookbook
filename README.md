# NeevCloud Cookbook

Recipes for building AI agents you can trust with a real computer. Every recipe runs on [NeevCloud](https://neevcloud.com) sandboxes, isolated Linux machines with an MCP server, a command audit trail, network egress you control, and memory snapshots you can roll back, plus NeevCloud's own models.

Each recipe runs as written: one install, one command, and one or two API keys. For the full product documentation, see [docs.ai.neevcloud.com](https://docs.ai.neevcloud.com/).

## Prerequisites

- Two NeevCloud API keys, one **Sandboxes** and one **Model API** ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key))
- Your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project)
- Python 3.11+ for Python recipes, or Node 20.3+ for TypeScript recipes
- Git

New to this, or on Windows? The [setup guide](docs/setup.md) covers installing Python and Node, the commands for each system, and common errors.

## Quickstart

On macOS or Linux ([Windows commands](docs/setup.md#windows)):

```bash
git clone https://github.com/NeevCloudAI/neev-cookbook.git
cd neev-cookbook/recipes/prompt-to-live-app-python
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python app_builder.py "a pomodoro timer with a calm green theme"
```

In about a minute you get a public URL to an app an agent built inside a sandbox. Every recipe works the same way: its README lists what it needs, and it deletes everything it creates when it ends.

<p align="center">
  <img src="assets/prompt-to-live-app.png" alt="The pomodoro timer the agent built from the quickstart command, open on its public preview URL" width="560">
  <br>
  <sub>The pomodoro timer the agent built from the command above, open on its preview URL.</sub>
</p>

## Recipes

### Build things with an agent

| Recipe | What you get | Python | TypeScript |
| --- | --- | --- | --- |
| **Prompt to live app** | Describe an app; an agent builds it in a sandbox and returns a public URL | [Python](recipes/prompt-to-live-app-python) | [TypeScript](recipes/prompt-to-live-app-js) |
| **AI data analyst** | Ask a question about a CSV; an agent answers with pandas in a sandbox cut off from the internet | [Python](recipes/ai-data-analyst-python) | [TypeScript](recipes/ai-data-analyst-js) |
| **Report generator** | Turn a CSV into a finished PDF and XLSX with a full Linux toolchain you never install | [Python](recipes/report-generator-python) |  |
| **Fix the failing test** | An agent fixes the code, never the tests, and hands back a patch verified in a fresh sandbox | [Python](recipes/fix-failing-test-python) |  |
| **Code mode over MCP** | One `run_python` tool against tool calls on the same question: fewer calls, fewer tokens, right answer | [Python](recipes/code-mode-mcp-python) |  |

### MCP and coding agents

| Recipe | What you get | Python | TypeScript |
| --- | --- | --- | --- |
| **Claude Code, Cursor or Codex over MCP** | Give your coding agent a disposable Linux machine with one URL, then read back what it did | [Guide and scripts](recipes/claude-code-over-mcp) |  |
| **One MCP URL, a crew of isolated agents** | A planner, a coder and a tester, each in its own sandbox, each in the audit trail | [Python](recipes/mcp-agent-crew-python) |  |
| **MCP agent with an undo button** | The agent snapshots before a risky migration, sees its tests fail and rolls itself back | [Python](recipes/mcp-agent-undo-python) |  |
| **Quarantine an untrusted MCP server** | Run a third-party MCP server with no internet and see what it tries to read, send and start | [Python](recipes/quarantine-mcp-server-python) |  |
| **Hosted coding agent** | Run OpenCode as a hosted agent on NeevCloud models, check its fix, pause and resume it | [Python](recipes/hosted-agent-python) |  |

### Audit and review

| Recipe | What you get | Python | TypeScript |
| --- | --- | --- | --- |
| **"What did my agent do?" session report** | A timeline of an agent's session from the audit trail, with sensitive reads flagged | [Python](recipes/agent-session-report-python) |  |
| **Audit evidence pack** | Every recorded operation per API key, exported to CSV and Markdown with a SHA-256 manifest | [Python](recipes/audit-evidence-pack-python) |  |
| **Human review gate** | See an agent's diff and its audit trail side by side, then approve or reject the change | [Python](recipes/human-review-gate-python) | [TypeScript](recipes/human-review-gate-js) |

### Security and egress

| Recipe | What you get | Python | TypeScript |
| --- | --- | --- | --- |
| **Prompt-injection-proof agent** | A poisoned README tells the agent to leak `.env`; the egress allow-list stops it either way | [Python](recipes/injection-proof-agent-python) | [TypeScript](recipes/injection-proof-agent-js) |
| **Supply-chain-safe installs** | A malicious package tries to phone home during install; only the package index is reachable | [Python](recipes/supply-chain-safe-installs-python) |  |
| **Live egress approval** | The agent asks for each host it needs and a person approves it into the live allow-list | [Python](recipes/egress-approval-python) |  |
| **Run untrusted user code in your SaaS** | A small service that runs your users' code, one sandbox per user, with limits and cleanup |  | [TypeScript](recipes/untrusted-code-runner-js) |
| **Code grader** | Grade untrusted submissions in parallel, one sandbox each, with cheating and network access blocked | [Python](recipes/code-grader-python) |  |

### Snapshots, forks and rollback

| Recipe | What you get | Python | TypeScript |
| --- | --- | --- | --- |
| **Undo the agent's mistake** | An agent deletes your data; one rollback restores the files, the running server and its memory | [Python](recipes/undo-agent-mistake-python) | [TypeScript](recipes/undo-agent-mistake-js) |
| **Best-of-N with fork** | Fork a sandbox three times, race three agents on a bug, keep the first fix that passes the tests | [Python](recipes/best-of-n-fork-python) |  |
| **Skip setup with a golden snapshot** | Set up once, then start every worker from a snapshot with packages, data and a warm service | [Python](recipes/golden-snapshot-python) |  |
| **Debug at the failure point** | A job fails; fork it at that moment and let an agent find the cause in the live process | [Python](recipes/debug-at-failure-python) |  |
| **Eval rollouts from one golden snapshot** | Compare models on tasks where every rollout starts from the same golden state | [Python](recipes/eval-rollouts-python) |  |

### Long-lived sandboxes

| Recipe | What you get | Python | TypeScript |
| --- | --- | --- | --- |
| **An agent that sleeps** | Pause between bursts of work and wake with the same process and its memory | [Python](recipes/agent-that-sleeps-python) |  |
| **Coding tutor with a shared box** | A student and an AI tutor share one sandbox: SSH, a preview URL and pause between sessions | [Python](recipes/coding-tutor-python) |  |

## Integrations

Use NeevCloud sandboxes from the agent framework you already have.

| Example | What it shows |
| --- | --- |
| [hello-world-python](examples/hello-world-python) | Create a sandbox, run a command, delete it |
| [hello-world-js](examples/hello-world-js) | Create a sandbox, run a command, delete it |
| [langchain-python](examples/langchain-python) | A LangChain agent with sandbox-backed tools |
| [langgraph-python](examples/langgraph-python) | A two-agent LangGraph crew, a sandbox each, over one MCP URL |
| [crewai-python](examples/crewai-python) | Replace CrewAI's removed code execution with a sandbox tool |
| [openai-agents-sdk-python](examples/openai-agents-sdk-python) | An OpenAI Agents SDK agent with a sandbox tool |
| [vercel-ai-sdk-js](examples/vercel-ai-sdk-js) | A Vercel AI SDK agent with a sandbox tool |
| [network-egress-python](examples/network-egress-python) | An egress allow-list: one host reachable, everything else blocked |
| [playwright-python](examples/playwright-python) | A headless browser in a sandbox, with the screenshot pulled back |

## Connecting

Any MCP client, such as Claude Code, Cursor or Codex, connects with a URL and an API key. There is
nothing to install and no bridge process to run:

```
https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp
```

Send your key as a bearer token and name the sandbox you want to work in:

```
Authorization: Bearer <NEEV_API_KEY>
x-sandbox-name: my-agent
```

That one connection covers everything: create a sandbox, run commands, read
and write files, start and manage processes, expose ports, snapshot, and roll back. The sandbox name binds the connection to one machine, so
giving each agent its own name gives each agent its own isolated environment.
[MCP setup](https://docs.ai.neevcloud.com/getting-started/mcp/overview) in the docs covers the server and how to
connect [Cursor](https://docs.ai.neevcloud.com/getting-started/mcp/connect-cursor) and [Codex](https://docs.ai.neevcloud.com/getting-started/mcp/connect-codex).

The SDKs and the CLI are the other way in if you would rather call the platform
directly:

- [neev-sdk-python](https://github.com/NeevCloudAI/neev-sdk-python)
- [neev-sdk-js](https://github.com/NeevCloudAI/neev-sdk-js)
- [neev-cli](https://github.com/NeevCloudAI/neev-cli) ([docs](https://docs.ai.neevcloud.com/getting-started/neev-cli))

## Models

NeevCloud serves models too, so you do not need a second provider. The recipes use
NeevCloud's OpenAI-compatible endpoint, and the framework examples take any
OpenAI-compatible endpoint through `NEEV_MODEL_BASE_URL`:

```
https://inference.ai.neevcloud.com/v1
```

```bash
curl https://inference.ai.neevcloud.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $NEEV_MODEL_API_KEY" \
  -d '{"model": "glm-4-7", "messages": [{"role": "user", "content": "hello"}]}'
```

Use an API key with Resource Type **Model API**. A Sandboxes key is not accepted by the model endpoint. See the [Model API docs](https://docs.ai.neevcloud.com/ai-inference/overview-1) for authentication, limits and pricing.

Models available when this was written: `glm-5-2`, `glm-4-7`, `deepseek-v3-2`, `kimi-k3`, `minimax-m3`,
`minimax-m2.7`, `minimax-m2.7-highspeed`, `gpt-oss-120b`, `gpt-oss-20b`,
`llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `gemma-4-31b`. The [Model API docs](https://docs.ai.neevcloud.com/ai-inference/overview-1) have the current list.

The recipes default to `glm-4-7`, which answers tool calls quickly and reliably. Set `MODEL` to try
another (`--models` in the eval rollouts recipe, `NEEV_MODEL` in the framework examples). Reasoning models spend tokens thinking before they answer, so give them headroom:
a low `max_tokens` returns an empty message and no tool call.

## What a sandbox gives you

The [Sandbox docs](https://docs.ai.neevcloud.com/agentic-studio/overview) cover each of these in detail.

- **A full Linux machine with root.** Install packages, run servers, keep a filesystem.
- **A network boundary you control.** Egress denies everything by default; you
  allow the domains your agent actually needs ([Internet access and egress](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).
- **Pause and resume.** Pause a sandbox between bursts of work and resume it with
  its files, memory and running processes as they were.
- **Snapshot and rewind.** Capture a sandbox, fork it, or roll it back when an
  agent breaks something ([Snapshots](https://docs.ai.neevcloud.com/agentic-studio/overview/snapshots)).
- **An audit trail.** Every operation (a command, a file read or write, a process
  start) is recorded against the key that ran it, without its arguments, so the
  trail is safe to keep.

## Repository layout

```
recipes/      one folder per recipe, each self-contained with its own README and tests
examples/     framework integrations and short single-feature examples
docs/         the setup guide for macOS, Linux and Windows
assets/       screenshots and run recordings used by the READMEs
.github/      issue and pull request templates, and the nightly check that runs every recipe and example
```

## Contributing

Recipes and examples are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) describes what every recipe does
(cleanup on every path, bounded agents, real output only) and how to verify one before opening a pull
request. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Report vulnerabilities privately to **security@neevcloud.com** or through
[GitHub private vulnerability reporting](https://github.com/NeevCloudAI/neev-cookbook/security/advisories/new), not in a public issue.
See [SECURITY.md](SECURITY.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
