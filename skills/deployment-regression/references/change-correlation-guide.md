# Change correlation guide

This guide explains how to correlate failures with recent deployments when investigating a suspected deployment regression.

## Identifying candidate deployments

A candidate deployment is one whose rollout timestamp falls within or just before the incident window. Check:

- **Deployment timestamps**: The exact time each version was deployed to production.
- **Rollout duration**: Gradual rollouts may cause a slow increase in error rate rather than a sudden spike.
- **Configuration changes**: Non-code changes (feature flags, environment variables, resource limits) can also introduce regressions.

## Temporal correlation analysis

1. Plot the error rate or latency metric on a timeline.
2. Overlay deployment timestamps on the same timeline.
3. Look for a change in the metric that coincides with the deployment.
4. Account for rollout duration: a gradual rollout means the effect builds over time.

## Comparing pre and post behavior

To confirm a regression:

1. Identify a baseline period before the deployment (same time of day, same day of week if possible).
2. Compare error rates, latency distributions, and log patterns between baseline and post-deployment.
3. Look for new error types or patterns that did not exist in the baseline.

## Excluding false correlations

Not every deployment that precedes a failure caused it:

- Check if the same failure occurred before the deployment.
- Verify that the failure affects only the services or endpoints changed in the deployment.
- Consider whether an external factor (traffic pattern change, downstream service change) coincided with the deployment.

## What this evidence supports

Deployment correlation data is one of the three required evidence types for the deployment-regression hypothesis. It must be combined with at least one additional independent source (logs or traces) to meet the minimum evidence threshold.
