"""Create a sandbox, run a command in it, delete it."""

from neevai import NeevAI

client = NeevAI()

# Creates in the org and project resolved from NEEV_ORG_ID / NEEV_PROJECT_ID.
sandbox = client.sandboxes.create({"name": "hello-world"})

# Everything after the create belongs in the try: a sandbox holds quota from the
# moment it exists, so any failure in between must still delete it.
try:
    sandbox.wait_until_ready()
    print(f"sandbox {sandbox.id} is {sandbox.phase}")

    result = sandbox.exec("sh", args=["-lc", "uname -sr && echo hello from inside"])
    print(result.stdout.strip())
finally:
    sandbox.delete()
    print("deleted")
