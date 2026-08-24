"""Per-request request-ID propagation.

The middleware accepts an inbound ``X-Request-ID`` when it matches a small
printable-token alphabet, otherwise it generates a ``req_``-prefixed
replacement.  The chosen ID is stored at ``scope["state"]["request_id"]``
(surfaced via ``request.state.request_id``) and always echoed on the response.
"""

from __future__ import annotations

import re
import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_HEADER_NAME_BYTES = b"x-request-id"

#: Printable token alphabet shared with common IDaaS gateways.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def is_valid_request_id(value: str | None) -> bool:
    """True when *value* may be echoed as an inbound request ID."""
    return value is not None and bool(_REQUEST_ID_PATTERN.match(value))


def generate_request_id() -> str:
    """Produce a fresh ``req_``-prefixed request ID."""
    return "req_" + secrets.token_hex(12)


class RequestIdMiddleware:
    """Pure-ASGI middleware assigning a stable per-request ID."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope.get("headers") or ())

        # ``Request.state`` reads ``scope["state"]``; seed it before dispatch so
        # route handlers and error handlers can rely on it.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(
                    name.lower() == _HEADER_NAME_BYTES for name, _ in headers
                ):
                    headers.append(
                        (_HEADER_NAME_BYTES, request_id.encode("latin-1"))
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_request_id)

    @staticmethod
    def _resolve_request_id(headers: list[tuple[bytes, bytes]]) -> str:
        for name, value in headers:
            if name.lower() == _HEADER_NAME_BYTES:
                inbound = value.decode("latin-1")
                if is_valid_request_id(inbound):
                    return inbound
                break
        return generate_request_id()
