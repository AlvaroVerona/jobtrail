"""Protection for a server that only ever talks to its owner's browser.

Binding to 127.0.0.1 is not enough: any web page you visit can make your browser send a
request to http://127.0.0.1:8000, and a DNS-rebinding page can even read the answer. Two
cheap checks close both holes:

* the Host header must be a loopback name (or the configured host);
* a state-changing request must not come from another origin.
"""

from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "testserver")


def install(app: FastAPI, *, extra_hosts: tuple[str, ...] = ()) -> None:
    @app.middleware("http")
    async def block_cross_origin_writes(request: Request, call_next):
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked", status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*LOOPBACK_HOSTS, *extra_hosts])
