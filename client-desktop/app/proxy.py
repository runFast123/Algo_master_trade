"""Transparent proxy from the local UI server to the backend API.

Hop-by-hop headers are dropped in both directions: forwarding them (especially
``content-length`` and ``transfer-encoding``) corrupts the response when the
body is re-framed by this server.
"""

import logging

import httpx
from fastapi import Request, Response

from .config import desktop_config

logger = logging.getLogger("proxy")

# Per RFC 9110, these are connection-scoped and must not be forwarded.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
STRIP_REQUEST = HOP_BY_HOP | {"host", "content-length"}
STRIP_RESPONSE = HOP_BY_HOP | {"content-length", "content-encoding"}


async def proxy_to_backend(request: Request, path: str) -> Response:
    url = f"{desktop_config.BACKEND_URL.rstrip('/')}/api/{path}"
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in STRIP_REQUEST
    }

    try:
        async with httpx.AsyncClient(timeout=desktop_config.PROXY_TIMEOUT) as client:
            upstream = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                params=dict(request.query_params),
                content=body,
            )
    except httpx.TimeoutException:
        logger.warning("Backend timed out for %s %s", request.method, url)
        return Response(
            content=b'{"detail":"The backend did not respond in time."}',
            status_code=504,
            media_type="application/json",
        )
    except httpx.HTTPError as exc:
        logger.warning("Backend unreachable for %s %s: %s", request.method, url, exc)
        return Response(
            content=b'{"detail":"The local backend service is not running."}',
            status_code=502,
            media_type="application/json",
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in STRIP_RESPONSE
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )
