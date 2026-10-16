// A Vercel AI SDK agent working in a NeevCloud sandbox over the sandbox MCP server.
import { randomUUID } from 'node:crypto'
import { pathToFileURL } from 'node:url'
import { experimental_createMCPClient as createMCPClient } from '@ai-sdk/mcp'
import { createOpenAI } from '@ai-sdk/openai'
import { generateText, stepCountIs } from 'ai'
import { TASK } from './agent.js'

const REGION = process.env.NEEV_REGION ?? 'as-south-1'
export const MCP_URL = `https://mcp.sandboxes.${REGION}.ai.neevcloud.com/mcp`

// The server also publishes lifecycle tools such as create_sandbox and delete_sandbox; the model gets only these.
export const AGENT_TOOLS = ['exec', 'fs_read', 'fs_write', 'fs_exists', 'fs_move', 'fs_remove']
if (process.env.ALLOW_PROCESSES === '1') {
  AGENT_TOOLS.push('process_start', 'process_get', 'process_logs', 'process_kill', 'process_list')
}

// Streamable HTTP transport bound to one sandbox by the x-sandbox-name header.
export function transport(apiKey, sandboxName) {
  return {
    type: 'http',
    url: MCP_URL,
    headers: { Authorization: `Bearer ${apiKey}`, 'x-sandbox-name': sandboxName },
  }
}

// The subset of the published tools the model may call.
export function agentTools(all) {
  return Object.fromEntries(AGENT_TOOLS.filter((n) => n in all).map((n) => [n, all[n]]))
}

// Calls a lifecycle tool the model never sees; throws if the server reports an error.
export async function hostCall(all, name) {
  const result = await all[name].execute({}, { toolCallId: `host-${name}`, messages: [] })
  if (result.isError) throw new Error(`${name}: ${result.content.map((c) => c.text ?? '').join('')}`)
  return result
}

// Deletes the sandbox, logging a failure so it cannot replace the error that ended the run.
export async function cleanup(all) {
  try {
    await hostCall(all, 'delete_sandbox')
    console.log('\n  sandbox deleted')
  } catch (err) {
    console.error(`\n  could not delete the sandbox: ${err.message}`)
    // A sandbox left behind keeps costing quota, so the run still exits non-zero.
    process.exitCode = 1
  }
}

// Creates a sandbox over MCP, runs the agent in it, then deletes it.
async function main() {
  const apiKey = process.env.NEEV_API_KEY
  if (!apiKey) throw new Error('NEEV_API_KEY is not set')

  // Any OpenAI-compatible endpoint works; this defaults to NeevCloud inference.
  const model = createOpenAI({
    baseURL: process.env.NEEV_MODEL_BASE_URL ?? 'https://inference.ai.neevcloud.com/v1',
    apiKey: process.env.NEEV_MODEL_API_KEY,
  }).chat(process.env.NEEV_MODEL ?? 'glm-5-2')

  const name = `ai-sdk-mcp-${randomUUID().slice(0, 8)}`
  const client = await createMCPClient({ transport: transport(apiKey, name) })
  try {
    const all = await client.tools()
    // A create that fails part-way can still leave a sandbox, so the delete covers it too.
    try {
      await hostCall(all, 'create_sandbox')
      console.log(`  sandbox ready: ${name}`)
      const { text } = await generateText({
        model,
        // Without a step limit the loop stops after the first tool call.
        stopWhen: stepCountIs(12),
        system: 'You have a Linux sandbox. Use the tools rather than guessing.',
        prompt: TASK,
        tools: agentTools(all),
      })
      console.log(`\n  agent: ${text.trim().slice(0, 200)}`)
    } finally {
      await cleanup(all)
    }
  } finally {
    await client.close()
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) await main()
