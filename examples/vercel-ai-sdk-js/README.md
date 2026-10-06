# Vercel AI SDK with NeevCloud Sandboxes

An AI SDK agent whose one tool runs commands on a real Linux machine.

## Run it

```bash
npm install

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...
export NEEV_MODEL_API_KEY=...

npm start
```

On Windows, set the keys with the PowerShell lines in [Setting up a recipe](../../README.md#setting-up-a-recipe).

The model endpoint defaults to `https://inference.ai.neevcloud.com/v1`; override
it with `NEEV_MODEL_BASE_URL` for any other OpenAI-compatible provider.

Output:

```
  sandbox ready: ai-sdk-mue2z8as

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
