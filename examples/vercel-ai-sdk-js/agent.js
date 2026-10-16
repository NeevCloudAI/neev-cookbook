// A Vercel AI SDK agent whose tools are the NeevCloud sandbox tools.
import { toAiSdk } from '@neevcloud/sdk/agent-tools/ai-sdk'
import { generateText, stepCountIs } from 'ai'

export const TASK =
  'Write primes.py that prints the first 10 prime numbers, run it with python3, ' +
  'and reply with exactly what it printed.'

// Runs TASK on model; throws the first sandbox error instead of letting the model retry it.
export async function runAgent(tools, model) {
  const { tools: aiTools, guard } = toAiSdk(tools)
  try {
    const { text } = await generateText({
      model,
      // Without a step limit the loop stops after the first tool call.
      stopWhen: stepCountIs(12),
      system: 'You have a Linux sandbox. Use the tools rather than guessing.',
      prompt: TASK,
      tools: aiTools,
      abortSignal: guard.signal,
    })
    return text
  } finally {
    // A failed sandbox call aborts the run; this rethrows its error instead of the abort.
    guard.raiseIfFailed()
  }
}
