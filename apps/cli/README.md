# IncidentLens CLI

IncidentLens CLI is an interactive terminal client for cloud investigations and durable Agent Sessions.

## Requirements

- Node.js `>=22.19.0`
- An IncidentLens control-plane URL and token

## Install

```bash
npm install -g @incidentlens/cli
incidentlens --version
```

The package contains the compiled CLI and its public runtime dependencies; it does not require a local IncidentLens checkout.

## Authentication

Set a token through the environment for non-interactive use:

```bash
export INCIDENTLENS_API_URL=https://incidentlens.example.com
export INCIDENTLENS_TOKEN=your-token
incidentlens
```

The CLI never accepts or stores private-key plaintext. Target setup uses a server-side authentication reference such as an SSH agent or managed credential. On systems where the optional OS keyring dependency cannot be loaded, the CLI reports that secure credentials are unavailable and asks for a safe authentication setup instead of printing a stack trace.

## Commands

- `/target` — configure, select, or test a remote target
- `/cancel` — cancel the active Agent operation
- `/exit` — leave the CLI
- `Ctrl+C` — interrupt the local terminal process

Natural-language prompts create or continue a server-side Agent Session. The terminal renders safe summaries of agent text, progress, tools, hypotheses, evidence, and child activity.

## Development

From the repository root:

```bash
npm ci
npm run verify:cli
npm pack --workspace @incidentlens/cli --dry-run
```

The package `prepack` hook checks protocol drift, type safety, tests, and the production bundle before a tarball is created.
