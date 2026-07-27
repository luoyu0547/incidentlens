"""Context propagation utilities for distributed tracing.

Every hop in the call chain must read/write:
  - X-Request-ID: unique per request
  - X-Trace-ID: shared across all hops for a single trace
"""

from __future__ import annotations

import uuid


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return f"req-{uuid.uuid4().hex[:16]}"


def generate_trace_id() -> str:
    """Generate a unique trace ID."""
    return f"trace-{uuid.uuid4().hex[:16]}"


def extract_context(headers: dict[str, str]) -> dict[str, str]:
    """Extract X-Request-ID and X-Trace-ID from incoming headers.

    If either header is missing, a new one is generated.
    Returns a dict with both headers set.
    """
    request_id = headers.get("x-request-id") or generate_request_id()
    trace_id = headers.get("x-trace-id") or generate_trace_id()
    return {
        "X-Request-ID": request_id,
        "X-Trace-ID": trace_id,
    }


def propagate_headers(context: dict[str, str]) -> dict[str, str]:
    """Build outgoing headers dict from context for the next hop.

    Returns headers with X-Request-ID and X-Trace-ID set.
    """
    return {
        "X-Request-ID": context["X-Request-ID"],
        "X-Trace-ID": context["X-Trace-ID"],
    }
