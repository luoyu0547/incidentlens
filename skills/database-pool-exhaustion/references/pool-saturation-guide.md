# Pool saturation guide

This guide explains how to interpret database connection pool metrics and logs when investigating pool exhaustion symptoms.

## Connection pool basics

A connection pool maintains a fixed set of database connections that are shared across application threads. Key metrics include:

- **active_connections**: Currently checked-out connections.
- **idle_connections**: Available connections waiting to be used.
- **max_pool_size**: The configured maximum number of connections.
- **wait_timeout_ms**: How long a thread waits for a connection before failing.

## Identifying pool saturation

Pool exhaustion occurs when `active_connections` reaches `max_pool_size` and new requests must wait. Signs include:

- Wait time exceeding the configured timeout threshold.
- Application logs containing "unable to acquire connection" or "connection pool exhausted" messages.
- Request latency spiking in lockstep with connection wait time.

## Common causes

- **Slow queries**: A query that normally takes 10ms takes 10 seconds, holding its connection far longer than expected.
- **Connection leak**: A code path checks out a connection but fails to return it due to an unhandled exception.
- **Traffic spike**: Request volume exceeds the pool's throughput capacity even with normal query latencies.

## Reading pool metrics

When analyzing pool metrics, look for:

1. The ratio of active connections to max pool size over time.
2. Correlation between pool saturation and request error rates.
3. Whether the saturation is transient (brief spike) or sustained (flat at max).

## What this evidence supports

Pool saturation data is one of the three required evidence types for the database-pool-exhaustion hypothesis. It must be combined with at least one additional independent source (logs or traces) to meet the minimum evidence threshold.
