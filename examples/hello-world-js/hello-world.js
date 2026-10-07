// Create a sandbox, run a command in it, delete it.
import { randomBytes } from 'node:crypto'
import { Neev } from '@neevcloud/sdk'

const neev = new Neev({
  apiKey: process.env.NEEV_API_KEY,
  orgId: process.env.NEEV_ORG_ID,
  projectId: process.env.NEEV_PROJECT_ID,
})

// A random suffix keeps two runs from asking for the same name.
const sandbox = await neev.sandboxes.create({ name: `hello-world-js-${randomBytes(4).toString('hex')}` })

// Everything after the create belongs in the try: a sandbox holds quota from the
// moment it exists, so any failure in between must still delete it.
try {
  await sandbox.waitUntilReady()
  console.log(`sandbox ${sandbox.id} is ${sandbox.phase}`)

  const result = await sandbox.exec('sh', { args: ['-lc', 'uname -sm && echo hello from inside'] })
  console.log(result.stdout.trim())
} finally {
  await sandbox.delete()
  console.log('deleted')
}
