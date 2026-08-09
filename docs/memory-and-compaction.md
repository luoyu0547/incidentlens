# Memory and Compaction

## Overview

IncidentLens uses a memory system to provide investigation context across sessions.
Memory is divided into three categories:

- **Project Memories** (`project_memories`): Long-term organizational knowledge
- **Session Memories** (`session_memories`): Per-investigation records that decay
- **Long-term Memories** (`long_term_memories`): Distilled knowledge from sessions

## Memory Format

Each memory file follows this structure:

```markdown
# [MEMORY_TYPE] title

- scope: project | team | service | global
- tags: [tag1, tag2]
- confidence: 0.0 ~ 1.0
- embedding: [float] (optional)

---

Memory content here.

---

## Source

- incident_id: xxx
- created_at: 2025-01-01T00:00:00Z
```

## Compaction

When session memories accumulate, the compaction system:

1. **Aggregates** memories by target service
2. **Detects** conflicting memories
3. **Distills** knowledge into long-term memories
4. **Decays** old, unused memories

### Configuration

```yaml
compaction:
  enabled: true
  token_threshold: 10000
  similarity_threshold: 0.85
  decay_rate: 0.1
  min_confidence: 0.3
```

### Fallback

When the compaction service is unavailable, the system falls back to summary mode.
The `summary_fallback_count` metric tracks how often this occurs.

## Evaluation Metrics

The evaluation framework tracks memory-related metrics:

| Metric | Description |
|--------|-------------|
| `project_memories_loaded` | Number of project memories loaded for an investigation |
| `compaction_count` | Number of compaction events triggered |
| `summary_fallback_count` | Number of fallbacks to summary mode |

## Strategy Comparison

| Strategy | Memory | LLM | Use Case |
|----------|--------|-----|----------|
| `deterministic_baseline` | No | No | Baseline without memory |
| `llm_agent` | Yes | Yes | Full memory pipeline |
