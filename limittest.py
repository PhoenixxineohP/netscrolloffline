"""Measure how fast a source actually lets us download, and where it pushes back.

Run from the project root::

    python limittest.py                     # quick check, 1 episode per source
    python limittest.py --episodes 3        # more samples
    python limittest.py --target webtoon    # only one source
    python limittest.py --concurrency 1,2,4,8

The tool only reads pages you could already read in a browser. It reports,
per concurrency level: requests made, HTTP status mix, wall time, pages/s and
MiB/s, plus any 429/503 responses with the delay the server asked for. Use it
to pick a safe slot count and ``img_delay`` instead of guessing.

Exit code is 0 when no rate-limit responses were seen at any level.
"""
import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import net
import sources
import webtoon

DEFAULT_TARGETS = {
    "webtoon": {
        "page": "https://www.webtoons.com/en/fantasy/tower-of-god/episode-{n}/viewer"
                "?title_no=95&episode_no={n}",
        "episodes": 1,
    },
}


def _webtoon_probe(episode):
    """Discover page image URLs for one episode (one HTML request)."""
    url = DEFAULT_TARGETS["webtoon"]["page"].format(n=episode)
    r = net.get(url, headers={"Referer": "https://www.webtoons.com/en/"}, attempts=2)
    return webtoon.VIEWER_IMG_RE.findall(r.text)


def _time_fetch(urls, concurrency, referer=None, label="", raw=False):
    """Fetch all urls with a fixed concurrency and record stats.

    With ``raw=True`` the shared per-host limiter is bypassed so the origin's
    own threshold becomes visible instead of our configured ceiling. That is
    impolite on purpose and only meant for a short, explicit test.
    """
    stats = {"codes": {}, "bytes": 0, "errors": 0, "retry_after": [], "times": []}

    def one(url):
        started = time.time()
        try:
            if raw:
                import requests
                r = requests.get(url, timeout=60, headers={
                    "User-Agent": net.UA,
                    **({"Referer": referer} if referer else {}),
                })
                took = time.time() - started
                stats["codes"][r.status_code] = stats["codes"].get(r.status_code, 0) + 1
                if r.status_code == 200:
                    stats["bytes"] += len(r.content)
                else:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        stats["retry_after"].append(ra)
                stats["times"].append(took)
                return r.status_code
            r = net.get(url, referer=referer, attempts=2, timeout=60,
                        allow_status=(429, 503))
            took = time.time() - started
            stats["codes"][r.status_code] = stats["codes"].get(r.status_code, 0) + 1
            if r.status_code == 200:
                stats["bytes"] += len(r.content)
            elif r.status_code in (429, 503):
                ra = r.headers.get("Retry-After")
                if ra:
                    stats["retry_after"].append(ra)
            stats["times"].append(took)
            return r.status_code
        except Exception:
            stats["errors"] += 1
            stats["times"].append(time.time() - started)
            return None

    started = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, urls))
    wall = max(1e-6, time.time() - started)
    ok = stats["codes"].get(200, 0)
    result = {
        "label": label,
        "concurrency": concurrency,
        "requests": len(urls),
        "ok": ok,
        "codes": stats["codes"],
        "errors": stats["errors"],
        "retry_after": stats["retry_after"][:3],
        "wall": wall,
        "rps": len(urls) / wall,
        "ok_rps": ok / wall,
        "mib_s": stats["bytes"] / (1024 * 1024) / wall,
        "p50_ms": (statistics.median(stats["times"]) * 1000) if stats["times"] else 0,
        "p95_ms": (
            sorted(stats["times"])[max(0, int(len(stats["times"]) * 0.95) - 1)] * 1000
            if stats["times"] else 0),
    }
    return result


def _print(result):
    codes = ", ".join(f"{k}×{v}" for k, v in sorted(result["codes"].items())) or "-"
    print(f"  concurrency {result['concurrency']:>2}  "
          f"req={result['requests']:>3}  ok={result['ok']:>3}  [{codes}]"
          f"{'  err=' + str(result['errors']) if result['errors'] else ''}")
    print(f"      {result['ok_rps']:.2f} pages/s   {result['mib_s']:.2f} MiB/s   "
          f"wall {result['wall']:.1f}s   p50 {result['p50_ms']:.0f}ms "
          f"p95 {result['p95_ms']:.0f}ms")
    if result["retry_after"]:
        print(f"      !! rate limited, Retry-After: {result['retry_after']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="webtoon",
                    choices=("webtoon", "mangadex", "tapas", "all"))
    ap.add_argument("--episodes", type=int, default=1)
    ap.add_argument("--concurrency", default="1,2,4,8")
    ap.add_argument("--mangadex-id", default="",
                    help="MangaDex manga uuid to test instead of the default sample")
    ap.add_argument("--tapas-id", default="1583128",
                    help="Tapas episode id to test")
    ap.add_argument("--raw", action="store_true",
                    help="bypass our own rate limiter to find the origin's ceiling")
    args = ap.parse_args(argv)
    levels = [int(x) for x in args.concurrency.split(",") if x.strip().isdigit()]

    db.init_db()
    limited = False
    print(f"Rate-limit test · levels={levels} · episodes={args.episodes}")

    if args.target in ("webtoon", "all"):
        print("\nWEBTOON (www.webtoons.com HTML + webtoon-phinf.pstatic.net images)")
        urls = []
        for ep in range(1, args.episodes + 1):
            pages = _webtoon_probe(ep)
            urls.extend(pages)
            print(f"  episode {ep}: {len(pages)} pages discovered")
        if urls:
            referer = DEFAULT_TARGETS["webtoon"]["page"].format(n=1)
            for level in levels:
                res = _time_fetch(urls, level, referer=referer, raw=args.raw)
                _print(res)
                limited = limited or bool(res["retry_after"]) or res["codes"].get(429)

    if args.target in ("mangadex", "all"):
        print("\nMangaDex (api.mangadex.org + mangadex.network images)")
        manga_id = args.mangadex_id or "dc07afaa-643a-4bf4-a9d0-b21ecc101736"
        try:
            chapters = sources.mangadex_chapters(manga_id, limit=max(1, args.episodes))
        except Exception as exc:
            print(f"  could not list chapters: {exc}")
            chapters = []
        urls = []
        for ch in chapters:
            try:
                pages, _ = sources.mangadex_pages(ch["id"])
            except Exception as exc:
                print(f"  chapter {ch['episode_no']}: {exc}")
                continue
            urls.extend(pages)
            print(f"  chapter {ch['episode_no']}: {len(pages)} pages discovered")
        for level in levels:
            res = _time_fetch(urls, level, raw=args.raw)
            _print(res)
            limited = limited or bool(res["retry_after"]) or res["codes"].get(429)

    if args.target in ("tapas", "all"):
        print("\nTapas (tapas.io HTML + us-a.tapas.io images)")
        try:
            urls = sources.tapas_pages(args.tapas_id)
            print(f"  episode {args.tapas_id}: {len(urls)} pages discovered")
        except Exception as exc:
            print(f"  could not load episode: {exc}")
            urls = []
        for level in levels:
            res = _time_fetch(urls, level, referer=f"https://tapas.io/episode/{args.tapas_id}",
                              raw=args.raw)
            _print(res)
            limited = limited or bool(res["retry_after"]) or res["codes"].get(429)

    print("\nLimiter state:")
    for host, snap in net.stats().items():
        if snap["inflight"] or snap["paused_for"]:
            print(f"  {host}: {snap}")
    if limited:
        print("\nResult: the origin pushed back. Lower --concurrency or raise img_delay.")
        return 1
    print("\nResult: no rate limiting observed at the tested levels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
