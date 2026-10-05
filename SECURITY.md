# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in a recipe or example here, or in the NeevCloud platform while
using one, report it privately. **Do not open a public GitHub issue for security reports.**

- Email **security@neevcloud.com** with details, or
- Use GitHub's [private vulnerability reporting](https://github.com/NeevCloudAI/neev-cookbook/security/advisories/new) for this repository.

Please include a description of the issue and its impact, steps to reproduce, and the recipe or
example involved.

We aim to acknowledge reports within **3 business days** and to provide a remediation timeline after
triage. We will coordinate a disclosure date with you and credit you unless you prefer to remain
anonymous.

## Supported versions

The recipes track the latest `0.x` releases of the NeevCloud SDKs (`neevai` for Python,
`@neevcloud/sdk` for TypeScript). Fixes land on `main`.

## Handling credentials

Every recipe reads its keys from environment variables (`NEEV_API_KEY`, `NEEV_MODEL_API_KEY`) and
never prints them or writes them into a sandbox. Keep it that way in your own code: never commit keys,
never embed them in client-side or browser code, and use a secrets manager in production.
Fixture "secrets" in the recipes (for example a `.env` written inside a sandbox) are dummy values.
