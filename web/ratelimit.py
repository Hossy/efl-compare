"""Abuse controls for the endpoints that cost real GPU time.

Reading a bill runs a multimodal model on someone's PDF. Serving JSON is free;
this is not. Published through a tunnel, /api/parse-bill is reachable by anyone
who learns the URL, so it needs three separate limits:

  * per-caller   -- a token bucket, so one person can't loop it
  * global       -- a concurrency cap, so a crowd can't queue the GPU to death
  * optional key -- a shared secret for private or programmatic use

Configuration (all optional, read from the environment or .env):

    EFL_RATE_PER_HOUR    bills per caller per hour      (default 12, 0 = off)
    EFL_RATE_BURST       how many can be back to back   (default 4)
    EFL_MAX_CONCURRENT   models running at once         (default 2, 0 = off)
    EFL_API_TOKEN        require X-API-Key to match     (default unset = open)
    EFL_TRUST_PROXY      1 when behind Cloudflare/nginx (default off)

On EFL_TRUST_PROXY
------------------
Behind a tunnel every request arrives from the tunnel itself, so the socket
address is the same for the whole internet -- rate limiting on it would put
every visitor in one bucket, letting a single abuser lock everybody out. The
real address is in CF-Connecting-IP / X-Forwarded-For.

Those headers are also trivially forged, and a forged one defeats the limiter
just as thoroughly. So they are honoured only when EFL_TRUST_PROXY says a proxy
is definitely in front and is definitely overwriting them. Off by default,
because trusting them when nothing strips them is worse than not having them.
"""

from __future__ import annotations

import os
import threading
import time

_DEF_PER_HOUR = 12.0
_DEF_BURST = 4.0
_DEF_CONCURRENT = 2
# A caller idle for this long is forgotten. Without it, one request per unique
# spoofed IP would grow the bucket dict without bound -- a memory leak wearing
# a rate limiter's coat.
_IDLE_EVICT_SECONDS = 3600.0
_MAX_TRACKED = 10_000


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return v if v >= 0 else default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


class RateLimiter:
    """Token bucket per caller. Thread-safe: ThreadingHTTPServer means concurrent calls."""

    def __init__(self, per_hour: float | None = None, burst: float | None = None):
        self.per_hour = _env_float("EFL_RATE_PER_HOUR", _DEF_PER_HOUR) if per_hour is None else per_hour
        self.burst = _env_float("EFL_RATE_BURST", _DEF_BURST) if burst is None else burst
        self._rate = self.per_hour / 3600.0          # tokens per second
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (tokens, last_seen)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_hour > 0 and self.burst > 0

    def _evict(self, now: float) -> None:
        """Drop idle callers. Caller must hold the lock."""
        dead = [k for k, (_, seen) in self._buckets.items() if now - seen > _IDLE_EVICT_SECONDS]
        for k in dead:
            del self._buckets[k]
        # Hard ceiling as a backstop: shed the stalest first.
        if len(self._buckets) > _MAX_TRACKED:
            for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: len(self._buckets) - _MAX_TRACKED]:
                del self._buckets[k]

    def check(self, key: str) -> tuple[bool, int]:
        """Spend a token for `key`. Returns (allowed, retry_after_seconds)."""
        if not self.enabled:
            return True, 0
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > _MAX_TRACKED // 2:
                self._evict(now)
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self._rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                # Time until one whole token exists again.
                wait = (1.0 - tokens) / self._rate if self._rate > 0 else _IDLE_EVICT_SECONDS
                return False, max(1, int(wait + 0.5))
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0


class ConcurrencyGuard:
    """Caps models running at once, so a crowd degrades politely instead of thrashing."""

    def __init__(self, limit: int | None = None):
        self.limit = _env_int("EFL_MAX_CONCURRENT", _DEF_CONCURRENT) if limit is None else limit
        self._sem = threading.BoundedSemaphore(self.limit) if self.limit > 0 else None

    def __enter__(self):
        self.acquired = self._sem.acquire(blocking=False) if self._sem else True
        return self.acquired

    def __exit__(self, *exc):
        if self._sem and self.acquired:
            self._sem.release()
        return False


def client_key(handler) -> str:
    """The caller's address, honouring proxy headers only when told to.

    Takes the *last* X-Forwarded-For entry rather than the first: a trusted
    proxy appends the address it saw, so the leftmost hop is whatever the client
    chose to claim, and the rightmost is the only one the proxy vouches for.
    """
    if os.environ.get("EFL_TRUST_PROXY", "").strip() not in ("", "0", "false", "no"):
        cf = handler.headers.get("CF-Connecting-IP")
        if cf and cf.strip():
            return cf.strip()
        xff = handler.headers.get("X-Forwarded-For")
        if xff and xff.strip():
            return xff.split(",")[-1].strip()
    try:
        return handler.client_address[0]
    except Exception:
        return "unknown"


def token_ok(handler) -> bool:
    """True when no token is configured, or the caller presented the right one."""
    want = os.environ.get("EFL_API_TOKEN", "").strip()
    if not want:
        return True
    got = (handler.headers.get("X-API-Key") or "").strip()
    if not got:
        auth = (handler.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    if len(got) != len(want):
        return False
    # Constant-time: a length-independent compare would leak the token a byte
    # at a time to anyone willing to measure.
    diff = 0
    for a, b in zip(got, want):
        diff |= ord(a) ^ ord(b)
    return diff == 0


def describe() -> str:
    """One line for the startup banner, so the active limits are never a guess."""
    rl = RateLimiter()
    cg = ConcurrencyGuard()
    bits = []
    bits.append(f"{rl.per_hour:g}/hr per caller (burst {rl.burst:g})" if rl.enabled else "per-caller limit OFF")
    bits.append(f"max {cg.limit} at once" if cg.limit > 0 else "concurrency OFF")
    bits.append("API key required" if os.environ.get("EFL_API_TOKEN", "").strip() else "no API key")
    trusted = os.environ.get("EFL_TRUST_PROXY", "").strip() not in ("", "0", "false", "no")
    bits.append("client IP from proxy headers" if trusted else "client IP from socket")
    return " · ".join(bits)
