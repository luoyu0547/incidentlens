# Memory Guidelines

Project Memory is **reference context** that helps the agent make better decisions
across sessions. It is not a log, not a cache, and not a secrets store.

## Rules

1. **No secrets.** Never store API keys, passwords, tokens, or connection strings.
2. **No current telemetry.** Memory captures durable knowledge, not transient
   runtime state or live metric snapshots.
3. **No hidden reasoning.** Every stored entry must have a human-readable
   description. Internal chain-of-thought or speculation is forbidden.
4. **One entry, one purpose.** Each memory file covers a single topic.

## Memory Types

| Type        | Purpose                                      |
|-------------|----------------------------------------------|
| project     | Architecture, conventions, design decisions  |
| procedure   | Step-by-step workflows and runbooks          |
| feedback    | Retrospective lessons learned from incidents |
| reference   | External links, API docs, configuration help |

## Naming

Memory file names must match the pattern `^[a-z][a-z0-9\-]{1,62}$`.
Use lowercase kebab-case. Examples: `api-conventions`, `deploy-runbook`.
