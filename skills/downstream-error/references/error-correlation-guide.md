# Error correlation guide

This guide explains how to correlate application errors across service boundaries when investigating downstream error symptoms.

## Error propagation patterns

When a downstream service encounters errors, they typically propagate upstream in one of these patterns:

- **Direct failure**: The upstream receives a 5xx response from the downstream and fails its own request.
- **Fallback failure**: The upstream attempts a fallback or circuit breaker, but the fallback also fails.
- **Cascading failure**: The downstream error causes resource exhaustion (connection pools, threads) that affects unrelated requests.

## Correlating errors across services

1. Identify the error signature in the downstream service (exception type, HTTP status code, error message pattern).
2. Search for the same or related error signature in the upstream service logs within the same time window.
3. Verify the temporal alignment: the downstream errors should begin at or before the upstream errors.
4. Check trace data to confirm the upstream error spans have child downstream error spans.

## Distinguishing cause from correlation

Not all correlated errors indicate causation. Check:

- **Temporal ordering**: Did the downstream errors start first?
- **Request-level linkage**: Can you trace specific upstream failures to specific downstream errors?
- **Volume correlation**: Does the upstream error rate track the downstream error rate?

## What this evidence supports

Error correlation data is one of the three required evidence types for the downstream-error hypothesis. It must be combined with at least one additional independent source (traces or metrics) to meet the minimum evidence threshold.
