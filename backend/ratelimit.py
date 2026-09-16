"""
PryMax Studio — rate limiting
==============================
A minimal sliding-window rate limiter with zero dependencies, keyed by
(client IP, limiter name). Good enough to stop a single client hammering
an endpoint; NOT a substitute for a real edge/WAF rate limiter in front of
a production deployment — this only protects this one process's memory,
and an attacker spreading requests across many IPs or many processes
isn't slowed down by it at all. See the caveat in ../README.md.

Usage:
    @rate_limit("chat", limit=20, window_seconds=10)
    @app.post("/api/room/<room_id>/chat")
    def post_chat(room_id): ...

Decorator order matters: @rate_limit must be *above* the Flask route
decorator (closer to the route) so Flask still sees a properly named
view function — functools.wraps handles that here.

Proxy trust (PRYMAX_TRUST_PROXY): secure by default. X-Forwarded-For is
trivially forgeable by anyone who can reach this process directly — if
this app is exposed straight to the internet (no reverse proxy in front
of it), trusting that header lets an attacker put any string they like in
it and get a fresh set of rate-limit counters on every request, defeating
the limiter entirely. So by default this only ever uses the actual TCP
connection's address (request.remote_addr), which cannot be forged the
same way. Only set PRYMAX_TRUST_PROXY=1 if this process genuinely sits
behind a reverse proxy that overwrites X-Forwarded-For itself (nginx,
Cloudflare, an API gateway) rather than passing through whatever the
client sent.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from functools import wraps
from threading import Lock

from flask import jsonify, request

from logging_config import log_warning
import logging

_lock = Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_logger = logging.getLogger("prymax")

TRUST_PROXY = os.environ.get("PRYMAX_TRUST_PROXY", "").lower() in ("1", "true", "yes")


def get_client_ip() -> str:
    """Public helper so other modules (app.py's request logging) apply the
    exact same proxy-trust rule as the rate limiter, instead of each
    re-implementing (and potentially disagreeing on) the same logic."""
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _client_key() -> str:
    return get_client_ip()


def rate_limit(name: str, limit: int, window_seconds: float):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (name, _client_key())
            now = time.monotonic()
            with _lock:
                hits = _hits[key]
                cutoff = now - window_seconds
                while hits and hits[0] < cutoff:
                    hits.popleft()
                if len(hits) >= limit:
                    retry_after = max(0.0, window_seconds - (now - hits[0]))
                    log_warning(
                        _logger,
                        "rate limit exceeded",
                        limiter=name,
                        client=_client_key(),
                        path=request.path,
                        request_id=getattr(request, "request_id", None),
                    )
                    resp = jsonify(
                        {"error": f"rate limit exceeded for '{name}' — try again shortly"}
                    )
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(int(retry_after) + 1)
                    return resp
                hits.append(now)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
