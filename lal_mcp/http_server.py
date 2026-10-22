"""Streamable-HTTP mode: run lal-mcp as a long-lived local service.

The client (e.g. Codex with `url = "http://127.0.0.1:8765/mcp"`) connects to it
instead of launching it, so the client's sandbox never has to start Python.

Safety: `run_lal` executes Python, so
  - the default bind address is 127.0.0.1, where the SDK also enables DNS-rebinding
    protection (only localhost Host/Origin headers are accepted);
  - binding anything else requires a bearer token (`--token-env NAME`);
  - with a token, every HTTP request must carry `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging

log = logging.getLogger("lal_mcp.http")

LOOPBACK_NAMES = {"localhost"}


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class BearerAuth:
    """ASGI middleware: reject HTTP requests without the right bearer token."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                body = json.dumps({"error": "missing or wrong bearer token"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)  # lifespan and authorized requests


async def serve(app, host: str, port: int, token: str | None, log_level: str) -> None:
    import uvicorn

    asgi = app.streamable_http_app(host=host)  # the SDK adds DNS-rebinding protection on loopback
    if token:
        asgi = BearerAuth(asgi, token)
    uv_level = {"TRACE": "trace", "DEBUG": "info"}.get(log_level.upper(), log_level.lower())
    config = uvicorn.Config(asgi, host=host, port=port, log_level=uv_level, log_config=None)
    log.info("listening on http://%s:%d/mcp%s", host, port, " (bearer token required)" if token else "")
    await uvicorn.Server(config).serve()
