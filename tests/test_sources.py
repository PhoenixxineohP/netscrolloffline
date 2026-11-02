"""Offline tests for the source layer, rate limiter and download scanner.

Run from the project root:  python tests/test_sources.py
No network access: HTTP is stubbed so these run anywhere.
"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_TMP = tempfile.mkdtemp(prefix="nso-sources-test-")
os.environ["NSO_DATA_DIR"] = _TMP

import db
db.init_db()

import net
import scanner
import sources

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail!r}")


# ---------------------------------------------------------------- URL detection
check("detect: english webtoon",
      sources.detect("https://www.webtoons.com/en/fantasy/tower-of-god/list?title_no=95")
      == {"source": "webtoon",
          "parts": {"lang": "en", "genre": "fantasy", "slug": "tower-of-god",
                    "title_no": "95"}})
check("detect: regional locale pt-br",
      sources.detect("https://www.webtoons.com/pt-br/romance/x/list?title_no=1")
      ["parts"]["lang"] == "pt-br")
check("detect: zh-hant locale",
      sources.detect("https://www.webtoons.com/zh-hant/romance/x/list?title_no=1")
      ["parts"]["lang"] == "zh-hant")
check("detect: viewer url works",
      sources.detect("https://www.webtoons.com/en/fantasy/tower-of-god/episode-1/viewer"
                     "?title_no=95&episode_no=1")["source"] == "webtoon")
md = sources.detect("https://mangadex.org/title/dc07afaa-643a-4bf4-a9d0-b21ecc101736/x")
check("detect: mangadex title", md["source"] == "mangadex"
      and md["manga_id"] == "dc07afaa-643a-4bf4-a9d0-b21ecc101736")
checked_bad = False
try:
    sources.detect("https://mangadex.org/title/not-a-uuid")
except ValueError:
    checked_bad = True
check("detect: mangadex bad id raises", checked_bad)
checked_bad = False
try:
    sources.detect("https://example.com/series/1")
except ValueError:
    checked_bad = True
check("detect: unsupported site raises", checked_bad)

tp = sources.detect("https://tapas.io/series/Stardust1")
check("detect: tapas series", tp["source"] == "tapas"
      and tp["parts"]["slug"] == "Stardust1")
check("detect: tapas /info suffix",
      sources.detect("https://tapas.io/series/Stardust1/info")["parts"]["slug"]
      == "Stardust1")
checked_bad = False
try:
    sources.parse_tapas_url("https://tapas.io/episode/123")
except ValueError:
    checked_bad = True
check("detect: tapas episode url rejected", checked_bad)

# ---------------------------------------------------------------- rate limiter
lim = net.RateLimiter(rps=20, burst=1, inflight=1)
start = time.time()
for _ in range(6):
    lim.acquire()
    lim.release()
elapsed = time.time() - start
check("limiter: spaces requests to the rate", elapsed >= 0.20, elapsed)

lim2 = net.RateLimiter(rps=1000, burst=2, inflight=2)
lim2.acquire()
lim2.acquire()
held = []


def try_third():
    lim2.acquire()
    held.append(True)


t = threading.Thread(target=try_third, daemon=True)
t.start()
time.sleep(0.12)
check("limiter: respects in-flight cap", not held)
lim2.release()
lim2.release()
t.join(timeout=2)
check("limiter: releases waiters when a slot frees", bool(held))

lim3 = net.RateLimiter(rps=1000, burst=5, inflight=5)
lim3.acquire()
lim3.release(retry_after=0.25)
start = time.time()
lim3.acquire()
lim3.release()
check("limiter: honours retry_after pause", time.time() - start >= 0.2)

# ---------------------------------------------------------------- scanner
d = tempfile.mkdtemp(prefix="nso-scan-")


def write(name, data):
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


good_jpg = write("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 5000)
check("scanner: accepts a real jpeg", scanner.verify_page(good_jpg, "jpg")[0])
good_png = write("b.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000)
check("scanner: accepts a real png", scanner.verify_page(good_png, "png")[0])
good_webp = write("c.webp", b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 5000)
check("scanner: accepts a real webp", scanner.verify_page(good_webp, "webp")[0])
html = write("d.jpg", b"<!DOCTYPE html><html>403</html>" + b" " * 5000)
ok, reason = scanner.verify_page(html, "jpg")
check("scanner: rejects an html error page", not ok and "image" in reason, reason)
tiny = write("e.jpg", b"\xff\xd8\xff" + b"x" * 10)
check("scanner: rejects a truncated file", not scanner.verify_page(tiny)[0])
empty = write("f.jpg", b"")
check("scanner: rejects an empty file", not scanner.verify_page(empty)[0])

# download_to must not leave a file behind when validation fails
class FakeResp:
    def iter_content(self, size=65536):
        yield b"<!DOCTYPE html>not an image" + b" " * 5000


dest = os.path.join(d, "nested", "page.jpg")
ok, reason = scanner.download_to("http://x/y.jpg", dest, lambda u: FakeResp())
check("scanner: download_to rejects bad bytes", not ok, reason)
check("scanner: download_to leaves no file", not os.path.exists(dest))
check("scanner: download_to leaves no .part", not [
    f for f in os.listdir(os.path.dirname(dest)) if f.endswith(".part")])


class GoodResp:
    def iter_content(self, size=65536):
        yield b"\xff\xd8\xff\xe0" + b"\x00" * 5000


ok, reason = scanner.download_to("http://x/y.jpg", dest, lambda u: GoodResp())
check("scanner: download_to keeps valid bytes", ok and os.path.exists(dest), reason)

# Antivirus is optional: absence must not be reported as a failure
check("scanner: clamav detection returns bool",
      isinstance(scanner.antivirus_available(), bool))
if not scanner.antivirus_available():
    ok, reason = scanner.verify_page(good_jpg, scan=True)
    check("scanner: missing antivirus still validates", ok and "scanner" in reason,
          reason)

# ---------------------------------------------------------------- pure helpers
check("sources: locale list has regional codes",
      "pt-br" in sources.WEBTOON_LOCALES and "zh-hant" in sources.WEBTOON_LOCALES)
check("sources: genre url shape",
      sources.webtoon_genre_url("es", "romance", 2)
      == "https://www.webtoons.com/es/genres/romance?page=2")

# mangadex_chapters filters external/unavailable chapters (stubbed transport)
class JsonResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


feed = {"data": [
    {"id": "c1", "type": "chapter", "attributes": {
        "chapter": "1", "title": "one", "translatedLanguage": "en",
        "isUnavailable": False, "externalUrl": None, "pages": 10}},
    {"id": "c2", "type": "chapter", "attributes": {
        "chapter": "2", "title": "", "translatedLanguage": "en",
        "isUnavailable": True, "externalUrl": None, "pages": 5}},
    {"id": "c3", "type": "chapter", "attributes": {
        "chapter": "3", "title": "", "translatedLanguage": "en",
        "isUnavailable": False, "externalUrl": "https://elsewhere", "pages": 5}},
]}
orig_get = net.get
net.get = lambda *a, **k: JsonResp(feed)
try:
    chs = sources.mangadex_chapters("some-manga", language="en")
    check("mangadex: keeps readable chapters", [c["id"] for c in chs] == ["c1"])
    check("mangadex: chapter number parsed", chs[0]["episode_no"] == 1)

    at_home = {"baseUrl": "https://cdn.example", "chapter": {
        "hash": "abc", "data": ["1.png", "2.png"], "dataSaver": ["1s.jpg"]}}
    net.get = lambda *a, **k: JsonResp(at_home)
    urls, digest = sources.mangadex_pages("c1")
    check("mangadex: builds page urls",
          urls[0] == "https://cdn.example/data/abc/1.png" and len(urls) == 2)
    check("mangadex: data-saver variant", 
          sources.mangadex_pages("c1", data_saver=True)[0][0]
          == "https://cdn.example/data-saver/abc/1s.jpg")
finally:
    net.get = orig_get

# tapas_episodes: flags paywalled chapters instead of hiding them
tapas_feed = {"data": {"episodes": [
    {"id": 11, "scene": 1, "title": "free one", "free": True, "unlocked": False,
     "early_access": False, "mature": False},
    {"id": 12, "scene": 2, "title": "paid one", "free": False, "unlocked": False,
     "early_access": True, "mature": False},
], "pagination": {"has_next": False}}}


class TapasResp:
    status_code = 200
    text = ""

    def json(self):
        return tapas_feed


net.get = lambda *a, **k: TapasResp()
try:
    eps = sources.tapas_episodes("157924")
    check("tapas: lists every episode", len(eps) == 2, len(eps))
    check("tapas: keeps free flag", eps[0]["free"] is True)
    check("tapas: flags paid/early access", eps[1]["free"] is False
          and eps[1]["early_access"] is True)
    check("tapas: episode ids are strings", eps[0]["id"] == "11")
finally:
    net.get = orig_get


class TapasPageResp:
    status_code = 200
    text = ('<img class="content__img js-lazy" data-src='
            '"https://us-a.tapas.io/c/92/a.jpg?__token__=x&amp;v=4">'
            '<img class="content__img js-lazy" data-src='
            '"https://us-a.tapas.io/c/37/b.jpg?__token__=y&amp;v=4">')


net.get = lambda *a, **k: TapasPageResp()
try:
    pages = sources.tapas_pages("1583128")
    check("tapas: extracts page images", len(pages) == 2, pages)
    check("tapas: unescapes &amp; in urls",
          "&amp;" not in pages[0] and "?__token__=x&v=4" in pages[0], pages[0])
finally:
    net.get = orig_get


class TapasLockedResp:
    status_code = 200
    text = '<div class="js-have-to-sign">subscribe to read</div>'


net.get = lambda *a, **k: TapasLockedResp()
try:
    locked = False
    try:
        sources.tapas_pages("999")
    except sources.SourceError as exc:
        locked = "locked" in str(exc)
    check("tapas: locked episode raises SourceError", locked)
finally:
    net.get = orig_get



# ---------------------------------------------------------------- app wiring
import app

client = app.app.test_client()
check("app: library renders browse box", b"browsebox" in client.get("/").data)
check("app: queue renders", client.get("/queue").status_code == 200)
check("app: settings renders", client.get("/settings").status_code == 200)
limits = client.get("/api/limits").get_json()
check("app: limits endpoint shape",
      {"hosts", "check_delay", "img_delay", "scanning"} <= set(limits))
check("app: limits includes webtoon host", "www.webtoons.com" in limits["hosts"])
src = client.get("/api/sources").get_json()
check("app: sources lists all three",
      [s["id"] for s in src["sources"]] == ["webtoon", "mangadex", "tapas"])
check("app: settings saves scan toggles",
      client.post("/api/settings", data={"scan_downloads": "on",
                                         "check_delay": "1.0",
                                         "img_delay": "0.35"}).status_code == 302
      and app._setting_bool("scan_downloads"))
check("app: limit override validates host",
      client.post("/api/limits/host", data={"host": "nope", "rps": "5"}).status_code == 404)
check("app: limit override validates rps",
      client.post("/api/limits/host",
                  data={"host": "www.webtoons.com", "rps": "0"}).status_code == 400)
check("app: limit override applies",
      client.post("/api/limits/host",
                  data={"host": "www.webtoons.com", "rps": "3",
                        "burst": "6", "inflight": "6"}).status_code == 200
      and net.stats()["www.webtoons.com"]["rps"] == 3.0)

# Source column must exist and default correctly for old rows
conn = db.connect()
with conn:
    conn.execute("INSERT INTO series (title_no, language, slug, title) "
                 "VALUES ('zz-test', 'en', 's', 'T')")
row = conn.execute("SELECT source, source_ref FROM series WHERE title_no='zz-test'").fetchone()
check("db: series has source column", row["source"] == "webtoon")
check("db: db is in WAL mode",
      conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")
conn.close()

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
