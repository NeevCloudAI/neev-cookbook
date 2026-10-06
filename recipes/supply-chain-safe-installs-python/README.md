# Supply-chain-safe installs

Install an untrusted dependency inside a sandbox whose egress allows only the package registry. A
malicious package runs code at install time that reads a local secret and tries to POST it to its own
server — but that server is off the allow-list, so the connection never opens. A legitimate
`pip install` from PyPI succeeds through the same allow-list.

## What you need

- Python 3.11 or later
- A NeevCloud account with an API key from **Account > API Keys** with Resource Type **Sandboxes**
  (`NEEV_API_KEY`). This recipe runs no model, so it does not need a Model API key.
- Your organization and project IDs (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`)

## Run it

```bash
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python safe_install.py            # or: python safe_install.py <a-different-pypi-package>
```

The secret in the fixture is a dummy value written at runtime, and the collector host is never on the
allow-list, so nothing real is ever sent anywhere. The script exits `0` only when the install-time
phone-home was blocked (its connection to the collector never opened) **and** the legitimate package
installed from PyPI.

## What you see

```
1. Creating a sandbox (egress allow-list: only pypi.org, files.pythonhosted.org)...
2. Building the untrusted package in the sandbox (its setup.py phones home at install time)...
   Wrote evilpkg/ (setup.py + module) and a dummy secret-store/credentials
3. Installing the untrusted package (it tries to POST the secret to example.org)...
   pip install ./evilpkg: exit 0
   Phone-home attempted: read 34 secret bytes; the connection to example.org never opened, so 0 bytes left the sandbox (OSError: [Errno 101] Network is unreachable)
4. Installing a legitimate package from PyPI (requests)...
   pip install requests: exit 0
   import requests: 2.34.2
5. Audit trail of the pip runs (program, target, outcome; arguments are never recorded):
   pip3 - -> success
   pip3 - -> success
The boundary held: the install-time POST to example.org never connected (0 bytes sent), and requests installed from PyPI.
```

The untrusted install still *succeeds* — installing a package you do not trust is exactly the risk.
What the allow-list removes is its ability to call home: the `setup.py` reads the secret and attempts
the upload, but the TCP connection to a host that is not `pypi.org` or `files.pythonhosted.org` never
opens, so zero bytes leave the sandbox.

## How it works

The script holds the egress policy; the untrusted package only ever runs inside the sandbox, so its
install-time code cannot widen its own network access.

1. `client.sandboxes.create({...}, allow_egress=["pypi.org", "files.pythonhosted.org"])` starts an
   isolated Linux machine that can reach the package registry and nothing else. Hosts match by exact
   FQDN, with no wildcards; a blocked host still resolves in DNS and then fails to connect, which is why
   the phone-home gets `Network is unreachable` (or a timeout) rather than a DNS error.
2. `sandbox.files.write(...)` builds the untrusted package under `evilpkg/`: a `setup.py` whose build
   step reads `secret-store/credentials` and tries to reach `https://example.org/collect` to upload it,
   plus a dummy secret file. All of this is written from code and never committed.
3. `sandbox.exec("pip3", ["install", "--no-input", "./evilpkg"])` installs it. pip's build isolation
   pulls `setuptools` from the allowed registry, then runs `setup.py`, which tries to open a TCP
   connection to the collector and only uploads if it succeeds, recording the result in
   `/workspace/phone_home.json`. The script reads that marker back. The pass condition is that the
   connection never opened, not just that no HTTP response came back — a reachable collector that
   answered an error would still have received the secret.
4. `sandbox.exec("pip3", ["install", "--no-input", "--target", "/workspace/legit-lib", "requests"])`
   installs a normal dependency from PyPI into a fresh directory; a follow-up `python3 -c "import
   requests"` is printed as confirmation it is usable but does not gate the result, since a package's
   import name can differ from its PyPI name. The exit code `0` depends on the phone-home being blocked
   and this `pip install` exiting `0`.
5. `sandbox.audit(from_=...)` shows the `pip3` runs. The audit records the program name and whether it
   ran, never the arguments, so the trail is safe to keep without storing what was installed or where it
   tried to connect.

DNS is a separate channel: as noted above, names off the allow-list still resolve, so treat DNS as its
own exfiltration path if your threat model includes it. What this recipe proves is that a TCP connection
to a host off the allow-list never opens.

## Time and cost

Typically under a minute. You pay for the sandbox minutes while it runs and nothing else — there are no
model tokens, because this recipe uses no model.

## Cleanup

`sandbox.delete()` runs in a `finally` block, so the sandbox is removed when the script ends, fails, or
you press `Ctrl+C` after it has been created. If the process is killed outright, or interrupted during
creation itself before the handle comes back, delete any leftover `safe-install-` sandbox from the
console.
