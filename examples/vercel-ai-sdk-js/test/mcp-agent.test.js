import assert from 'node:assert/strict'
import { test } from 'node:test'
import { AGENT_TOOLS, MCP_URL, agentTools, cleanup, hostCall, transport } from '../mcp-agent.js'

// A published tool whose execute records its call and answers with isError.
const fakeTool = (calls, name, isError) => ({
  execute: async (args, options) => {
    calls.push([name, args, options.toolCallId])
    return { isError, content: [{ type: 'text', text: 'no sandbox is bound' }] }
  },
})

test('transport carries the key and picks the sandbox', () => {
  assert.deepEqual(transport('key', 'box'), {
    type: 'http',
    url: MCP_URL,
    headers: { Authorization: 'Bearer key', 'x-sandbox-name': 'box' },
  })
})

test('the model gets only the working tools', () => {
  const all = Object.fromEntries(
    ['exec', 'fs_read', 'fs_write', 'fs_list', 'fs_exists', 'fs_move', 'fs_remove', 'create_sandbox'].map((n) => [n, { name: n }]),
  )
  assert.deepEqual(Object.keys(agentTools(all)), AGENT_TOOLS)
  assert.deepEqual(AGENT_TOOLS, ['exec', 'fs_read', 'fs_write', 'fs_exists', 'fs_move', 'fs_remove'])
})

test('ALLOW_PROCESSES=1 adds the process tools', async () => {
  process.env.ALLOW_PROCESSES = '1'
  try {
    // A fresh module instance reads the variable again.
    const { AGENT_TOOLS: withProcesses } = await import('../mcp-agent.js?processes')
    assert.deepEqual(withProcesses.slice(6), ['process_start', 'process_get', 'process_logs', 'process_kill', 'process_list'])
  } finally {
    delete process.env.ALLOW_PROCESSES
  }
})

test('hostCall sends no arguments', async () => {
  const calls = []
  await hostCall({ create_sandbox: fakeTool(calls, 'create_sandbox', false) }, 'create_sandbox')
  assert.deepEqual(calls, [['create_sandbox', {}, 'host-create_sandbox']])
})

test('hostCall throws on a tool error', async () => {
  await assert.rejects(
    hostCall({ delete_sandbox: fakeTool([], 'delete_sandbox', true) }, 'delete_sandbox'),
    /delete_sandbox: no sandbox is bound/,
  )
})

test('a failed delete is logged, not thrown, so the original error survives', async (t) => {
  const logged = []
  t.mock.method(console, 'error', (msg) => logged.push(msg))
  await cleanup({ delete_sandbox: fakeTool([], 'delete_sandbox', true) })
  assert.match(logged[0], /could not delete the sandbox: delete_sandbox: no sandbox is bound/)
  assert.equal(process.exitCode, 1)
  process.exitCode = 0
})
