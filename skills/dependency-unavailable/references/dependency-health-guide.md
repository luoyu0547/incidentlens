# Dependency health guide

This guide explains how to interpret dependency health and connectivity data when investigating an unreachable service.

## Dependency health indicators

Service dependencies report health through several signals:

- **Health check status**: Binary up/down status from the dependency's health endpoint.
- **Connection success rate**: Percentage of outbound connections that succeed over a time window.
- **DNS resolution time**: Time to resolve the dependency's hostname. Elevated DNS latency can precede full unavailability.
- **TCP connect time**: Time to establish a TCP connection after DNS resolution.

## Identifying an unavailable dependency

An unavailable dependency typically shows:

1. Connection refused or connection reset errors in application logs.
2. Health check failures in the dependency monitoring system.
3. Traces with missing downstream spans where the upstream reports an error.

## Distinguishing network from application issues

Not all connection failures indicate a network partition:

- **Application crash**: The dependency is unreachable because the process is down. Health checks fail consistently.
- **Network partition**: The dependency may be running but unreachable from the calling service. Check from multiple vantage points.
- **Overloaded dependency**: The dependency is reachable but too busy to accept new connections. Connection timeouts may appear instead of immediate refusals.

## Partial availability

Some failures show partial availability where:

- Some instances of the dependency are healthy while others are down.
- Load balancing distributes requests across healthy and unhealthy instances.
- This creates intermittent failures rather than complete unavailability.

## What this evidence supports

Dependency health data is one of the three required evidence types for the dependency-unavailable hypothesis. It must be combined with at least one additional independent source (logs or traces) to meet the minimum evidence threshold.
