// An OpenAI Agents SDK (JavaScript) agent with the NeevCloud sandbox tools.
import { Neev } from '@neevcloud/sdk'
import { SandboxTools } from '@neevcloud/sdk/agent-tools'
import { toOpenAIAgents } from '@neevcloud/sdk/agent-tools/openai-agents'
import { Agent, run, setDefaultOpenAIClient, setOpenAIAPI, setTracingDisabled } from '@openai/agents'
import OpenAI from 'openai'

const TASK =
  'Write primes.py that prints the first 10 prime numbers, run it with python3, ' +
  'and reply with exactly what it printed.'

// Any OpenAI-compatible endpoint works; this defaults to NeevCloud inference.
setDefaultOpenAIClient(
  new OpenAI({
    baseURL: process.env.NEEV_MODEL_BASE_URL ?? 'https://inference.ai.neevcloud.com/v1',
    apiKey: process.env.NEEV_MODEL_API_KEY,
  }),
)
// Chat Completions works against any OpenAI-compatible endpoint; Responses is OpenAI-only.
setOpenAIAPI('chat_completions')
// No OpenAI account is involved, so there is no tracing backend to talk to.
setTracingDisabled(true)

const neev = new Neev()
const sandbox = await neev.sandboxes.create({ name: `oa-js-${Date.now().toString(36)}` })

// The sandbox holds quota from here, so the try starts here.
try {
  await sandbox.waitUntilReady()
  console.log(`  sandbox ready: ${sandbox.name}`)
  const tools = new SandboxTools(sandbox, {
    allowPreview: process.env.ALLOW_PREVIEW === '1',
    allowRollback: process.env.ALLOW_ROLLBACK === '1',
    allowProcesses: process.env.ALLOW_PROCESSES === '1',
  })
  const agent = new Agent({
    name: 'Sandbox engineer',
    instructions: 'You have a Linux sandbox. Use the tools rather than guessing.',
    model: process.env.NEEV_MODEL ?? 'glm-5-2',
    tools: toOpenAIAgents(tools),
  })
  const result = await run(agent, TASK, { maxTurns: 12 })
  console.log(`\n  agent: ${String(result.finalOutput).trim().slice(0, 200)}`)
} finally {
  await sandbox.delete()
  console.log('\n  sandbox deleted')
}
