import assert from 'node:assert/strict'
import { test } from 'node:test'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { AuthenticationError, BadRequestError } from '@neevcloud/sdk'
import { SandboxTools } from '@neevcloud/sdk/agent-tools'
import { runAgent } from '../agent.js'

// SandboxTools over a fake sandbox: reading boom.txt fails fatally, anything else is missing.
const tools = () =>
  new SandboxTools({
    files: {
      async read(path) {
        if (path === 'boom.txt') throw new AuthenticationError(401, { message: 'invalid api key for /orgs/org-123' }, undefined)
        throw new BadRequestError(400, { message: `${path}: no such file` }, undefined)
      },
    },
  })

// Connects an MCP client to the in-process server, the way the CLI reaches it.
async function connect(options) {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await options.mcpServers.neev.instance.connect(serverSide)
  const client = new Client({ name: 'test', version: '1.0.0' })
  await client.connect(clientSide)
  return client
}

// Replaces query(): Claude reads path once, then the turn ends with a result.
function fakeQuery(path, consumed, result = { type: 'result', subtype: 'success', result: 'done' }) {
  return ({ options }) => {
    const q = (async function* () {
      const call = {
        type: 'assistant',
        message: { content: [{ type: 'tool_use', id: 't1', name: 'mcp__neev__read_file', input: { path } }] },
      }
      consumed.push(call)
      yield call
      await (await connect(options)).callTool({ name: 'read_file', arguments: { path } })
      for (const msg of [{ type: 'user' }, result]) {
        consumed.push(msg)
        yield msg
      }
    })()
    q.closed = false
    q.close = () => {
      q.closed = true
    }
    consumed.query = q
    return q
  }
}

test('the run stops and rethrows the fatal error', async () => {
  const consumed = []
  await assert.rejects(runAgent(tools(), fakeQuery('boom.txt', consumed)), AuthenticationError)
  assert.ok(!consumed.some((m) => m.type === 'result'))
  assert.equal(consumed.query.closed, true)
})

test('the run returns the result when tools only report errors', async () => {
  const consumed = []
  assert.equal(await runAgent(tools(), fakeQuery('x.txt', consumed)), 'done')
  assert.equal(consumed.query.closed, true)
})

test('the run throws when it did not succeed', async () => {
  for (const result of [
    { type: 'result', subtype: 'error_max_turns', is_error: true, result: 'partial' },
    { type: 'result', subtype: 'success', is_error: true, result: 'partial' },
  ]) {
    await assert.rejects(runAgent(tools(), fakeQuery('x.txt', [], result)), {
      message: `agent run did not succeed: ${result.subtype}: partial`,
    })
  }
})
