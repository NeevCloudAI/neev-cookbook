import assert from 'node:assert/strict'
import { test } from 'node:test'
import { AuthenticationError, BadRequestError } from '@neevcloud/sdk'
import { SandboxTools } from '@neevcloud/sdk/agent-tools'
import { MockLanguageModelV2 } from 'ai/test'
import { runAgent } from '../agent.js'

// SandboxTools over a fake sandbox: reading boom.txt fails fatally, anything else is missing.
function fakeTools() {
  const reads = []
  const tools = new SandboxTools({
    files: {
      async read(path) {
        reads.push(path)
        if (path === 'boom.txt') throw new AuthenticationError(401, { message: 'invalid api key' }, undefined)
        throw new BadRequestError(400, { message: `${path}: no such file` }, undefined)
      },
    },
  })
  return { tools, reads }
}

const usage = { inputTokens: 1, outputTokens: 1, totalTokens: 2 }

// One model step that calls toolName with input.
const toolStep = (toolName, input) => ({
  content: [{ type: 'tool-call', toolCallId: 'call-1', toolName, input: JSON.stringify(input) }],
  finishReason: 'tool-calls',
  usage,
  warnings: [],
})

const textStep = (text) => ({ content: [{ type: 'text', text }], finishReason: 'stop', usage, warnings: [] })

// A model that plays steps in order and, like a real provider, rejects once the run is aborted.
function fakeModel(steps) {
  return new MockLanguageModelV2({
    doGenerate: async (options) => {
      if (options.abortSignal?.aborted) throw options.abortSignal.reason
      return steps.shift()
    },
  })
}

// The tool message the model saw on its second step.
const toolMessage = (model) => model.doGenerateCalls[1].prompt.find((m) => m.role === 'tool').content[0]

test('a thrown core error stops the run and is rethrown', async () => {
  const { tools, reads } = fakeTools()
  const model = fakeModel([toolStep('read_file', { path: 'boom.txt' }), textStep('done')])
  await assert.rejects(runAgent(tools, model), (err) => err instanceof AuthenticationError && err.message.includes('invalid api key'))
  assert.deepEqual(reads, ['boom.txt'])
  // The abort ends the loop before the model is asked again.
  assert.equal(model.doGenerateCalls.length, 1)
})

test('an error result reaches the model and the run continues', async () => {
  const { tools, reads } = fakeTools()
  const model = fakeModel([toolStep('read_file', { path: 'nope.txt' }), textStep('the file is missing')])
  assert.equal(await runAgent(tools, model), 'the file is missing')
  assert.deepEqual(reads, ['nope.txt'])
  const part = toolMessage(model)
  assert.equal(part.toolName, 'read_file')
  assert.deepEqual(part.output, { type: 'json', value: { error: 'HTTP 400: nope.txt: no such file' } })
})
