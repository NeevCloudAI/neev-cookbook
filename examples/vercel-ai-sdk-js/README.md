# Vercel AI SDK with NeevCloud Sandboxes

An AI SDK agent whose one tool runs commands on a real Linux machine.

<p align="center">
  <img src="../../assets/runs/vercel-ai-sdk-js.gif" alt="A real run of this example, recorded in a terminal" width="720">
</p>

## Run it

You need Node 20+, a **Sandboxes** and a **Model API** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)), and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project).

```bash
npm install

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_API_KEY=...

npm start
```

On Windows, see the [setup guide](../../docs/setup.md#windows) for the PowerShell commands.

The model endpoint defaults to `https://inference.ai.neevcloud.com/v1`; override
it with `NEEV_MODEL_BASE_URL` for any other OpenAI-compatible provider.

Output:

```
  sandbox ready: ai-sdk-muwuuryy

  agent: antelope

  sandbox deleted
```

## Notes

**Set a step limit.** Without `stopWhen: stepCountIs(n)` the SDK stops after the
first tool call and never comes back with a final answer. This is the single most
common thing to get wrong when giving an AI SDK agent tools.

```js
const { text } = await generateText({
  model,
  stopWhen: stepCountIs(8),
  tools: { run: tool({ ... }) },
})
```
