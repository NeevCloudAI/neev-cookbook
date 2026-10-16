// Starts a sandbox, runs the Vercel AI SDK agent against it, then deletes it.
import { createOpenAI } from '@ai-sdk/openai'
import { Neev } from '@neevcloud/sdk'
import { SandboxTools } from '@neevcloud/sdk/agent-tools'
import { runAgent } from './agent.js'

// Any OpenAI-compatible endpoint works; this defaults to NeevCloud inference.
const model = createOpenAI({
  baseURL: process.env.NEEV_MODEL_BASE_URL ?? 'https://inference.ai.neevcloud.com/v1',
  apiKey: process.env.NEEV_MODEL_API_KEY,
}).chat(process.env.NEEV_MODEL ?? 'glm-5-2')

const neev = new Neev()
const sandbox = await neev.sandboxes.create({ name: `ai-sdk-${Date.now().toString(36)}` })

// The sandbox holds quota from here, so the try starts here -- waitUntilReady can
// fail too, and that must not leak it.
try {
  await sandbox.waitUntilReady()
  console.log(`  sandbox ready: ${sandbox.name}`)
  const tools = new SandboxTools(sandbox, {
    allowPreview: process.env.ALLOW_PREVIEW === '1',
    allowRollback: process.env.ALLOW_ROLLBACK === '1',
    allowProcesses: process.env.ALLOW_PROCESSES === '1',
  })
  console.log(`\n  agent: ${(await runAgent(tools, model)).trim().slice(0, 200)}`)
} finally {
  await sandbox.delete()
  console.log('\n  sandbox deleted')
}
