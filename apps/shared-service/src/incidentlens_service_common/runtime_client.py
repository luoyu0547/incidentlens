"""RuntimeConfigClient — fetch active scenarios from the control plane.

Public interface:
  - RuntimeConfigClient(control_plane_url, service).get_active() -> dict[str, dict[str, Any]]

Key design:
  - Fetches /api/scenarios/runtime/{service} from the control plane
  - On timeout or any httpx.HTTPError, returns {} (no fault injected)
  - Uses a short timeout (2s by default) to avoid blocking request handling
  - Services use CONTROL_PLANE_URL env var in Compose mode
"""

from __future__ import annotations

from typing import Any

import httpx


class RuntimeConfigClient:
    """Client for fetching active runtime scenarios from the control plane.

    On any network error or unexpected response, returns an empty dict
    so that no fault is injected (graceful degradation).
    """

    def __init__(
        self,
        control_plane_url: str,
        service: str,
        timeout: float = 2.0,
    ) -> None:
        self._control_plane_url = control_plane_url.rstrip("/")
        self._service = service
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._control_plane_url,
                timeout=self._timeout,
            )
        return self._client

    async def get_active(self) -> dict[str, dict[str, Any]]:
        """Fetch active scenarios for this service from the control plane.

        Returns the 'active' dict from the runtime endpoint response.
        On any error (timeout, connection failure, HTTP error), returns {}.
        """
        try:
            client = self._get_client()
            response = await client.get(f"/api/scenarios/runtime/{self._service}")
            response.raise_for_status()
            data = response.json()
            return data.get("active", {})
        except httpx.HTTPError:
            return {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
