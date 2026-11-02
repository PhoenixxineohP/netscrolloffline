"""Shared polite HTTP layer.

Every download runs through :func:`get`, which

* applies a per-host token bucket so all threads/slots share one budget
  instead of each slot independently hammering the origin,
* honours ``Retry-After`` and ``X-RateLimit-Reset`` so a 429 slows the whole
  host down rather than just the unlucky request,
* retries transient failures with exponential backoff and jitter.

The limits below are deliberately conservative because they were measured
against the live origins; see ``README.md`` ("Rate limits and sources").
"""
import random
import threading
import time
import urllib.parse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Requests-per-second ceiling per host, and how many may be in flight.
# Measured behaviour: webtoons.com tolerates ~2 rps of page loads and starts
# returning 429/5xx around 8-10 rps; the pstatic CDN serves 16 parallel image
# fetches fine and degrades past ~24. MangaDex's API is documented at 5 rps
# global / 40 per minute per endpoint, so we stay well under it.
HOST_LIMITS = {
    "www.webtoons.com": {"rps": 2.0, "burst": 4, "inflight": 4},
    "m.webtoons.com": {"rps": 2.0, "burst": 4, "inflight": 4},
    "webtoon-phinf.pstatic.net": {"rps": 12.0, "burst": 16, "inflight": 16},
    "api.mangadex.org": {"rps": 3.0, "burst": 5, "inflight": 4},
    # MangaDex restricts /at-home/server to 40 requests/minute (~0.66/s)
    # separately from the global 5/s API budget, so it gets its own bucket.
    "api.mangadex.org|at-home": {"rps": 0.6, "burst": 2, "inflight": 2},
    "uploads.mangadex.org": {"rps": 10.0, "burst": 12, "inflight": 12},
    "mangadex.network": {"rps": 10.0, "burst": 12, "inflight": 12},
    "tapas.io": {"rps": 1.5, "burst": 3, "inflight": 3},
    "us-a.tapas.io": {"rps": 8.0, "burst": 12, "inflight": 12},
    "eu-a.tapas.io": {"rps": 8.0, "burst": 12, "inflight": 12},
    "img.tapas.io": {"rps": 8.0, "burst": 12, "inflight": 12},
}
DEFAULT_LIMIT = {"rps": 2.0, "burst": 3, "inflight": 3}

RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504, 522, 524)


class RateLimiter:
    """Token bucket + in-flight cap, shared by every thread for one host."""

    def __init__(self, rps=2.0, burst=3, inflight=3):
        self._rps = float(rps)
        self._capacity = float(max(burst, 1))
        self._tokens = float(max(burst, 1))
        self._inflight_cap = max(int(inflight), 1)
        self._inflight = 0
        self._updated = time.monotonic()
        self._paused_until = 0.0
        self._lock = threading.Condition()

    def configure(self, rps, burst, inflight):
        with self._lock:
            self._rps = float(rps)
            self._capacity = float(max(burst, 1))
            self._inflight_cap = max(int(inflight), 1)
            self._tokens = min(self._tokens, self._capacity)
            self._lock.notify_all()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rps)
            self._updated = now

    def acquire(self):
        with self._lock:
            while True:
                now = time.monotonic()
                if now < self._paused_until:
                    self._lock.wait(self._paused_until - now)
                    continue
                self._refill()
                if self._inflight < self._inflight_cap and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._inflight += 1
                    return
                wait = 0.05
                if self._tokens < 1.0 and self._rps > 0:
                    wait = max(wait, (1.0 - self._tokens) / self._rps)
                if self._inflight >= self._inflight_cap:
                    wait = max(wait, 0.1)
                self._lock.wait(min(wait, 1.0))

    def release(self, retry_after=None):
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if retry_after and retry_after > 0:
                self._paused_until = max(self._paused_until, time.monotonic() + retry_after)
            self._lock.notify_all()

    def snapshot(self):
        with self._lock:
            return {
                "rps": self._rps,
                "burst": int(self._capacity),
                "inflight": self._inflight,
                "inflight_cap": self._inflight_cap,
                "paused_for": max(0.0, self._paused_until - time.monotonic()),
            }


_LIMITERS = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(host: str) -> RateLimiter:
    """Return the shared bucket for a host, creating it on first use."""
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(host)
        if lim is None:
            cfg = HOST_LIMITS.get(host) or _config_for_host(host)
            lim = RateLimiter(cfg["rps"], cfg["burst"], cfg["inflight"])
            _LIMITERS[host] = lim
        return lim


def _config_for_host(host: str) -> dict:
    """Match at-home CDN hosts and unknown MangaDex edge hosts to a budget."""
    if host.endswith("mangadex.network") or host.endswith("mangadex.org"):
        return HOST_LIMITS.get("mangadex.network", DEFAULT_LIMIT)
    if host.endswith("tapas.io"):
        return HOST_LIMITS.get("us-a.tapas.io", DEFAULT_LIMIT)
    if host.endswith("pstatic.net"):
        return HOST_LIMITS.get("webtoon-phinf.pstatic.net", DEFAULT_LIMIT)
    if host.endswith("webtoons.com"):
        return HOST_LIMITS.get("www.webtoons.com", DEFAULT_LIMIT)
    return DEFAULT_LIMIT


def _bucket_key(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc
    # at-home has its own stricter quota on the same host as the API
    if host == "api.mangadex.org" and "/at-home/" in urllib.parse.urlsplit(url).path:
        return "api.mangadex.org|at-home"
    if host in HOST_LIMITS:
        return host
    return host  # unknown hosts share their own bucket via _config_for_host


def set_host_limit(host: str, rps: float, burst=None, inflight=None):
    """Adjust a host budget at runtime (used by the settings page / tests)."""
    cfg = HOST_LIMITS.get(host, dict(DEFAULT_LIMIT))
    cfg["rps"] = float(rps)
    if burst is not None:
        cfg["burst"] = int(burst)
    if inflight is not None:
        cfg["inflight"] = int(inflight)
    HOST_LIMITS[host] = cfg
    limiter_for(host).configure(cfg["rps"], cfg["burst"], cfg["inflight"])


def stats():
    """Current per-host budget and in-flight counts, for the Queue page."""
    return {h: limiter_for(h).snapshot() for h in sorted(HOST_LIMITS)}


def _retry_after(resp) -> float:
    """Seconds the server asked us to wait, from either common header."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return 0.0


def get(url, cookie=None, referer=None, headers=None, timeout=30,
        attempts=4, session=None, allow_status=()):
    """Rate-limited GET. Raises for status not in ``allow_status``."""
    host = _bucket_key(url)
    lim = limiter_for(host)
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        h["Referer"] = referer
    if cookie:
        h["Cookie"] = cookie
    if headers:
        h.update(headers)

    own_session = session is None
    s = session or requests.Session()
    last_exc = None
    try:
        for attempt in range(attempts):
            lim.acquire()
            try:
                r = s.get(url, headers=h, timeout=timeout)
            except requests.RequestException as exc:
                last_exc = exc
                lim.release()
                time.sleep(min(2 ** attempt, 20) * (0.5 + random.random()))
                continue

            if r.status_code in allow_status:
                lim.release()
                return r

            if r.status_code in RETRY_STATUS:
                wait = _retry_after(r) or min(2 ** attempt, 30)
                lim.release(retry_after=wait)
                last_exc = requests.HTTPError(
                    f"{r.status_code} for {url}", response=r)
                if attempt == attempts - 1:
                    break
                time.sleep(wait * (0.5 + random.random() * 0.5))
                continue

            lim.release()
            r.raise_for_status()
            return r

        if last_exc:
            raise last_exc
        raise requests.HTTPError(f"giving up on {url}")
    finally:
        if own_session:
            s.close()
