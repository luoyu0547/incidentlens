"""Fault scenario model definitions.

Five fault types:
  - payment_delay: adds real latency to payment-service responses
  - payment_error_rate: makes payment-service return 500 at a configurable rate
  - db_pool_exhaustion: simulates connection pool exhaustion on order-service
  - dependency_unavailable: makes order-service return 502 when calling payment
  - deployment_regression: simulates a buggy deployment on payment-service

Each scenario has:
  - target_service: which service the fault affects
  - root_cause_label: internal label (NOT exposed via API)
  - default_params: default fault parameters
"""

from __future__ import annotations

from typing import Any

# Scenario definitions: name -> definition dict
SCENARIOS: dict[str, dict[str, Any]] = {
    "payment_delay": {
        "target_service": "payment-service",
        "root_cause_label": "payment_latency_spike",
        "default_params": {"delay_ms": 200},
    },
    "payment_error_rate": {
        "target_service": "payment-service",
        "root_cause_label": "payment_service_degradation",
        "default_params": {"error_rate": 0.3},
    },
    "db_pool_exhaustion": {
        "target_service": "order-service",
        "root_cause_label": "database_connection_leak",
        "default_params": {"pool_size": 2},
    },
    "dependency_unavailable": {
        "target_service": "order-service",
        "root_cause_label": "network_partition",
        "default_params": {"dependency": "payment-service"},
    },
    "deployment_regression": {
        "target_service": "payment-service",
        "root_cause_label": "bad_deployment",
        "default_params": {"version": "v2.0.0-buggy"},
    },
}
