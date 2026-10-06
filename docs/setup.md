# Setup

Everything you need before running a recipe, on macOS, Linux or Windows.

- [1. Get your keys](#1-get-your-keys)
- [2. Install Python or Node](#2-install-python-or-node)
- [3. Set up a recipe](#3-set-up-a-recipe)
- [Troubleshooting](#troubleshooting)

## 1. Get your keys

In the NeevCloud console, open **Account > API Keys** and create:

- a key with Resource Type **Sandboxes**, used as `NEEV_API_KEY`
- a key with Resource Type **Model API**, used as `NEEV_MODEL_API_KEY`

Note your organization and project IDs too (`NEEV_ORG_ID`, `NEEV_PROJECT_ID`). Some recipes need only the Sandboxes key; each README says which.

Docs: [Create an API key](https://docs.ai.neevcloud.com/getting-started/create-api-key) · [Organizations and projects](https://docs.ai.neevcloud.com/getting-started/org-and-project)

## 2. Install Python or Node

Python recipes need **Python 3.11 or later**. TypeScript recipes need **Node 20.3 or later**.

Check what you have:

```bash
python3 --version     # Windows: py --version
node --version
```

If Python is too old, install a current one:

- **macOS**: the installer from [python.org](https://www.python.org/downloads/macos/). The Python built into macOS is too old.
- **Linux**: Ubuntu 24.04 and later already have 3.12. Add its venv module:
  ```bash
  sudo apt install python3.12-venv     # Ubuntu
  sudo dnf install python3.12          # Fedora
  ```
- **Windows**: the installer from [python.org](https://www.python.org/downloads/windows/), or:
  ```powershell
  winget install Python.Python.3.12
  ```

For Node, use the installer from [nodejs.org](https://nodejs.org/en/download) on any system.

The commands below use `python3.12` (on Windows, `py -3.12`). If you installed another version, use its name instead, such as `python3.14` or `py -3.14`.

## 3. Set up a recipe

Run these in the recipe's folder. Each recipe gets its own virtualenv, so its packages never mix with your system Python.

### macOS and Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export NEEV_API_KEY=... NEEV_MODEL_API_KEY=... NEEV_ORG_ID=... NEEV_PROJECT_ID=...
```

### Windows

In PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:NEEV_API_KEY = "..."
$env:NEEV_MODEL_API_KEY = "..."
$env:NEEV_ORG_ID = "..."
$env:NEEV_PROJECT_ID = "..."
```

Then run the recipe's command from its README, as written.

### TypeScript recipes

No virtualenv: run `npm install` in the recipe's folder, set the keys as above, then run the recipe's command from its README.

## Troubleshooting

### `No matching distribution found for neevai`

pip is running on a Python older than 3.10, such as the one built into macOS. Create the virtualenv with Python 3.11 or later and install again inside it.

### `ensurepip` error, then `pip: command not found`

That Python cannot install pip into a virtualenv. We have seen this with Homebrew builds of Python on recent macOS. Delete the `.venv` folder and create it again with a Python from python.org or your system's package manager.

### `Activate.ps1 cannot be loaded`

Allow local scripts for your user once, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
