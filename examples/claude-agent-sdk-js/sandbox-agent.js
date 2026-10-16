// Starts a sandbox, runs the Claude Agent SDK agent against it, then deletes it.
import { Neev } from '@neevcloud/sdk'
import { SandboxTools } from '@neevcloud/sdk/agent-tools'
import { runAgent } from './agent.js'

const neev = new Neev()
const sandbox = await neev.sandboxes.create({ name: `claude-js-${Date.now().toString(36)}` })

// The sandbox holds quota from here, so the try starts here.
try {
  await sandbox.waitUntilReady()
  console.log(`  sandbox ready: ${sandbox.name}`)
  const tools = new SandboxTools(sandbox, {
    allowPreview: process.env.ALLOW_PREVIEW === '1',
    allowRollback: process.env.ALLOW_ROLLBACK === '1',
    allowProcesses: process.env.ALLOW_PROCESSES === '1',
  })
  console.log(`\n  agent: ${(await runAgent(tools)).trim().slice(0, 200)}`)
} finally {
  await sandbox.delete()
  console.log('\n  sandbox deleted')
}
