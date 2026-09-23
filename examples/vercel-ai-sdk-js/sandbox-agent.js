// A Vercel AI SDK agent whose one tool is a NeevCloud sandbox.
import { createOpenAI } from '@ai-sdk/openai'
import { Neev } from '@neevcloud/sdk'
import { generateText, stepCountIs, tool } from 'ai'
import { z } from 'zod'

const neev = new Neev({
  apiKey: process.env.NEEV_API_KEY,
  orgId: process.env.NEEV_ORG_ID,
  projectId: process.env.NEEV_PROJECT_ID,
})

// Any OpenAI-compatible endpoint works; this defaults to NeevCloud inference.
const model = createOpenAI({
  baseURL: process.env.NEEV_MODEL_BASE_URL ?? 'https://inference.ai.neevcloud.com/v1',
  apiKey: process.env.NEEV_MODEL_API_KEY,
}).chat(process.env.NEEV_MODEL ?? 'glm-5-2')

const sandbox = await neev.sandboxes.create({ name: `ai-sdk-${Date.now().toString(36)}` })
await sandbox.waitUntilReady()
console.log(`  sandbox ready: ${sandbox.name}`)

try {
  const { text } = await generateText({
    model,
    // Without a step limit the loop stops after the first tool call.
    stopWhen: stepCountIs(8),
    system: 'You have a Linux sandbox. Run commands rather than guessing.',
    prompt:
      'Write a file words.txt with five animal names one per line, then use ' +
      'sort and head to print the alphabetically first one. Reply with that word only.',
    tools: {
      run: tool({
        description: 'Run a shell command in the sandbox. State persists between calls.',
        inputSchema: z.object({ command: z.string().describe('Shell command to run') }),
        execute: async ({ command }) => {
          const r = await sandbox.exec('sh', { args: ['-lc', command] })
          return r.exitCode === 0 ? r.stdout || '(no output)' : `exit ${r.exitCode}: ${r.stderr}`
        },
      }),
    },
  })
  console.log(`\n  agent: ${text.trim().slice(0, 150)}`)
} finally {
  await sandbox.delete()
  console.log('\n  sandbox deleted')
}
