// A Claude Agent SDK agent whose only tools are the NeevCloud sandbox tools. They
// run in-process as an SDK MCP server; Claude Code's built-in tools act on the
// machine running this script, so they are all switched off.
import { query } from '@anthropic-ai/claude-agent-sdk'
import { toClaude } from '@neevcloud/sdk/agent-tools/claude'

export const TASK =
  'Write primes.py that prints the first 10 prime numbers, run it with python3, ' +
  'and reply with exactly what it printed.'

// Runs TASK; throws the first sandbox error, a host tool's use, or a run that did not succeed.
export async function runAgent(tools, run = query) {
  const claude = toClaude(tools)
  const options = {
    ...claude.options(),
    systemPrompt: 'You have a Linux sandbox. Use the tools rather than guessing.',
    maxTurns: 12,
  }
  let final = ''
  let failed = null
  for await (const msg of claude.watch(run({ prompt: TASK, options }))) {
    if (msg.type === 'result') {
      final = msg.result ?? ''
      if (msg.is_error || msg.subtype !== 'success') failed = msg.subtype
    }
  }
  if (failed) throw new Error(`agent run did not succeed: ${failed}: ${final}`)
  return final
}
