# Multi-stage Docker build for IncidentLens services
# Builds all services in a single image, selected via SERVICE env var

# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy workspace configuration
COPY pyproject.toml uv.lock ./
COPY packages/contracts/ packages/contracts/
COPY packages/telemetry/ packages/telemetry/
COPY packages/scenarios/ packages/scenarios/
COPY packages/evaluation/ packages/evaluation/
COPY packages/demo/ packages/demo/
COPY apps/shared-service/ apps/shared-service/
COPY apps/gateway-service/ apps/gateway-service/
COPY apps/order-service/ apps/order-service/
COPY apps/payment-service/ apps/payment-service/
COPY apps/control-plane/ apps/control-plane/

# Install the workspace
RUN uv sync --frozen --no-dev

# ---- Runtime stage ----
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/packages /app/packages
COPY --from=builder /app/apps /app/apps
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

# Copy LLM agent config and skills
COPY config/ config/
COPY skills/ skills/

# Set path to use venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Service selection via SERVICE env var
# Options: gateway, order, payment, control-plane
ENV SERVICE=control-plane

# Default ports per service
ENV GATEWAY_PORT=8000
ENV ORDER_PORT=8001
ENV PAYMENT_PORT=8002
ENV CONTROL_PLANE_PORT=8003

# Expose all possible ports
EXPOSE 8000 8001 8002 8003

# Create data directory for SQLite
RUN mkdir -p /data && chmod 777 /data

# Entry point selects which service to run
CMD ["sh", "-c", "\
    mkdir -p /data && \
    case $SERVICE in \
        gateway) uvicorn gateway_service.main:app --host 0.0.0.0 --port ${GATEWAY_PORT} ;; \
        order) uvicorn order_service.main:app --host 0.0.0.0 --port ${ORDER_PORT} ;; \
        payment) uvicorn payment_service.main:app --host 0.0.0.0 --port ${PAYMENT_PORT} ;; \
        control-plane) uvicorn incidentlens_control_plane.main:app --host 0.0.0.0 --port ${CONTROL_PLANE_PORT} ;; \
        *) echo \"Unknown SERVICE: $SERVICE\" && exit 1 ;; \
    esac"]
