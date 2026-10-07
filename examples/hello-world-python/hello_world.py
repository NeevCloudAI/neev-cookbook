"""Create a sandbox, run a command in it, delete it."""

import uuid

from neevai import NeevAI

client = NeevAI()

# Creates in the org and project resolved from NEEV_ORG_ID / NEEV_PROJECT_ID.
# A random suffix keeps two runs from asking for the same name.
sandbox = client.sandboxes.create({"name": f"hello-world-{uuid.uuid4().hex[:8]}"})

# Everything after the create belongs in the try: a sandbox holds quota from the
# moment it exists, so any failure in between must still delete it.
try:
    sandbox.wait_until_ready()
    print(f"sandbox {sandbox.id} is {sandbox.phase}")

    result = sandbox.exec("sh", args=["-lc", "uname -sm && echo hello from inside"])
    print(result.stdout.strip())
finally:
    sandbox.delete()
    print("deleted")
