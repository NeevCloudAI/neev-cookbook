"""Create a sandbox, run a command in it, delete it."""

from neevai import NeevAI

client = NeevAI()

# Creates in the org and project resolved from NEEV_ORG_ID / NEEV_PROJECT_ID.
sandbox = client.sandboxes.create({"name": "hello-world"})
sandbox.wait_until_ready()
print(f"sandbox {sandbox.id} is {sandbox.phase}")

result = sandbox.exec("sh", args=["-lc", "uname -sr && echo hello from inside"])
print(result.stdout.strip())

# A sandbox holds quota until deleted, even once it pauses itself.
sandbox.delete()
print("deleted")
