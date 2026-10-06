# Network egress allow-list

A sandbox reaches only the hosts you name. This creates two sandboxes, one with an allow-list and one with the default policy, and checks from inside each what it can reach.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NEEV_API_KEY=...
export NEEV_ORG_ID=...
export NEEV_PROJECT_ID=...

python egress_allow_list.py
```

On Windows, use the PowerShell setup in [Setting up a recipe](../../README.md#setting-up-a-recipe) for the virtualenv and the keys.

Output:

```
  two sandboxes ready

  allow-listed sandbox -> example.com : True
  allow-listed sandbox -> wikipedia.org : False
  default sandbox      -> example.com : False

  both sandboxes deleted
```

- `{"egress": {"mode": "allow_list", "allow": [{"host": "example.com"}]}}` lets the sandbox reach exactly that host. Names match exactly; there are no wildcards.
- A sandbox created with no `egress` at all denies everything. That is the default, so code that is talked into sending data somewhere has nowhere to send it.
- A blocked host does not fail DNS; the connection simply times out, which is why the check uses a short `curl -m 8`.

Set `ALLOWED_HOST` or `BLOCKED_HOST` to try other hosts. Both sandboxes are deleted at the end, also when a check fails.

For the same boundary holding against a prompt-injected agent, see the [injection-proof agent recipe](../../recipes/injection-proof-agent-python).
