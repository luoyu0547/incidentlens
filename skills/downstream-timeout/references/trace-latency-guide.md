# Trace latency guide

This guide explains how to interpret distributed trace data when investigating downstream timeout symptoms.

## Reading trace spans

Each span in a trace represents a single unit of work within a service. The key fields for timeout analysis are:

- **duration**: Wall-clock time the span took. Compare this against the service's normal baseline.
- **status**: `OK`, `ERROR`, or `UNSET`. A span with `ERROR` status and high duration suggests a timeout or slow failure.
- **parent_id**: Links child spans to their parent. The upstream span's duration should roughly equal the sum of its children.

## Identifying slow downstream spans

1. Order spans by duration descending.
2. Look for a single downstream span whose duration accounts for more than 70% of the total trace duration.
3. Compare the span's duration against the P95 latency for that service endpoint in the incident window.

## Timeout patterns

- **Client-side timeout**: The upstream span ends with a deadline-exceeded error while the downstream span is still in progress.
- **Server-side timeout**: The downstream span ends with a timeout error and a short duration, indicating the server gave up.
- **Partial timeout**: Some requests succeed and some time out, indicating intermittent slowness rather than a hard timeout.

## Correlating with logs

When a timeout occurs, the downstream service typically logs:
- Request deadline exceeded messages.
- Connection pool exhaustion warnings if the timeout is caused by resource contention.
- Retry attempt logs if the upstream retries before timing out.

## What this evidence supports

Trace latency data is one of the three required evidence types for the downstream-timeout hypothesis. It must be combined with at least one additional independent source (logs or metrics) to meet the minimum evidence threshold.
