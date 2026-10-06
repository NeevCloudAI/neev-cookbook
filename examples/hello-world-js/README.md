# Hello World (JavaScript)

Create a sandbox, run a command, delete it.

<p align="center">
  <img src="../../assets/runs/hello-world-js.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

You need Node 20+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
npm install

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...

npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

Output:

```
sandbox 01a111e1-6c14-737f-b859-07e616a1bad8 is Ready
Linux x86_64
hello from inside
deleted
```

For the Python equivalent see [hello-world-python](../hello-world-python).
