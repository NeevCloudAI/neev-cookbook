// Create a sandbox, run a command in it, delete it.
import { Neev } from '@neevcloud/sdk'

const neev = new Neev({
  apiKey: process.env.NEEV_API_KEY,
  orgId: process.env.NEEV_ORG_ID,
  projectId: process.env.NEEV_PROJECT_ID,
})

const sandbox = await neev.sandboxes.create({ name: 'hello-world-js' })
await sandbox.waitUntilReady()
console.log(`sandbox ${sandbox.id} is ${sandbox.phase}`)

const result = await sandbox.exec('sh', { args: ['-lc', 'uname -sr && echo hello from inside'] })
console.log(result.stdout.trim())

// A sandbox holds quota until deleted, even once it pauses itself.
await sandbox.delete()
console.log('deleted')
