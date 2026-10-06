# Supply-chain-safe installs

Install a dependency you don't trust inside a sandbox that can reach only the package registry. Here a malicious package runs code at install time that reads a local secret and tries to send it to its own server. That server isn't on the allow-list, so the connection never opens, while a normal `pip install` from PyPI still works.

<p align="center">
  <img src="../../assets/runs/supply-chain-safe-installs-python.gif" alt="A real run of this recipe, recorded in a terminal" width="720">
</p>

## Run it

You need Python 3.11+, a **Sandboxes** key ([create a key](https://docs.ai.neevcloud.com/getting-started/create-api-key)) and your [organization and project IDs](https://docs.ai.neevcloud.com/getting-started/org-and-project). No model key: no model is involved.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
python safe_install.py
```

On Windows, see the [setup guide](../../docs/setup.md#windows).

Pass a package name to install a different legitimate package from PyPI: `python safe_install.py httpx`. The secret is a dummy value and the collector is never on the allow-list, so nothing real is sent anywhere. The script exits 0 only when the phone-home was blocked and the legitimate package installed.

## How it works

1. **Allow-list.** The script starts a sandbox that can reach only `pypi.org` and `files.pythonhosted.org`.
2. **Malicious package.** It builds a package in the sandbox whose `setup.py` reads a dummy secret at install time and tries to upload it to `example.org`.
3. **Install it.** `pip install ./evilpkg` succeeds, which is exactly the risk: install-time code runs. But its connection to `example.org` never opens, so 0 bytes leave. The package records its own attempt, and the script reads that record back.
4. **Install a real package.** `pip install requests` works through the same allow-list.
5. **Audit.** The audit trail shows both `pip3` runs, never their arguments.

## Use it in your product

- **Agents that install packages:** give the agent's sandbox an allow-list of just your package registries, so whatever it installs can't call home ([Internet access](https://docs.ai.neevcloud.com/agentic-studio/overview/internet-access)).
- **CI for untrusted code:** build and test pull requests from outside contributors in a sandbox like this one.
- **Your own registry:** replace `ALLOWED_HOSTS` in `safe_install.py` with your private index or mirror.

## Good to know

- Hosts match by exact name, with no wildcards.
- The check is that the connection never opened, not just that no response came back: a collector that answered with an error would still have received the secret.
- A blocked host still resolves in DNS and then fails to connect. Treat DNS as a separate channel if your threat model includes it.

## Time and cost

Usually under a minute, and only sandbox time: there is no model. The sandbox is deleted when the script ends, fails or you press `Ctrl+C`. If the process is killed outright, delete any leftover `safe-install-` sandbox from the console.
