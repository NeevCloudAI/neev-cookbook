# NeevCloud Cookbook

Working examples for running AI agents on [NeevCloud](https://neevcloud.com)
sandboxes — real Linux machines your agent can drive, isolated from everything
else and from each other.

Each example runs as written. Set your API key and go.

## Examples

| Example | What it shows |
| --- | --- |
| [hello-world-python](examples/hello-world-python) | Create a sandbox, run a command, delete it |
| [crewai-python](examples/crewai-python) | Replace CrewAI's removed code execution with a sandbox tool |
| [langgraph-python](examples/langgraph-python) | A two-agent LangGraph crew where each agent gets its own sandbox, over one MCP URL |

## Connecting

There is no CLI to install and no bridge process to run. Any MCP client connects
with a URL and an API key:

```
https://mcp.sandboxes.as-south-1.ai.neevcloud.com/mcp
```

Send your key as a bearer token and name the sandbox you want to work in:

```
Authorization: Bearer <NEEV_API_KEY>
x-sandbox-name: my-agent
```

That single connection carries the whole surface — create a sandbox, run
commands, read and write files, start and manage processes, expose ports,
snapshot, and roll back. The sandbox name binds the connection to one machine, so
giving each agent its own name gives each agent its own isolated environment.

The Python and JavaScript SDKs are the other way in if you would rather call the
platform directly:

- [neev-sdk-python](https://github.com/NeevCloudAI/neev-sdk-python)
- [neev-sdk-js](https://github.com/NeevCloudAI/neev-sdk-js)
- [neev-cli](https://github.com/NeevCloudAI/neev-cli)

## What a sandbox gives you

- **Root on a real machine.** Install packages, run servers, keep a filesystem.
- **A network boundary you control.** Egress denies everything by default; you
  allow the domains your agent actually needs.
- **Sleep and resume.** An idle sandbox suspends and stops billing, then resumes
  with its processes still running.
- **Snapshot and rewind.** Capture a sandbox, fork it, or roll it back when an
  agent breaks something.
- **An audit trail.** Every command, file and terminal session is recorded
  against the key that ran it.

## Contributing

Examples are welcome. Keep them runnable end to end, have them clean up the
sandboxes they create, and put the setup steps in a README next to the code.

## License

Apache 2.0. See [LICENSE](LICENSE).
