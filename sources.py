"""Catalogue sources beyond the WEBTOON web reader.

The app started as webtoons.com-only. This module makes a series' origin
explicit so extra catalogues can be added without touching the download job.

Sources
-------
``webtoon``
    Naver/LINE WEBTOON web reader (``www.webtoons.com``), including its other
    language editions (``/es/``, ``/fr/``, ``/de/``, ``/th/``, ``/id/``,
    ``/zh-hant/``, ``/pt-br/``). Same code path, different ``language``.
``mangadex``
    MangaDex's official, documented JSON API (``api.mangadex.org``). English
    scanlations of the same titles, plus content WEBTOON never hosts. Uses
    ``/manga`` search, ``/manga/{id}/feed`` for chapters and ``/at-home/server``
    for page images. No login or API key is required for public chapters.

Deliberately **not** included: sites that redistribute paid content without
permission, and reverse-engineered private app APIs. Both are what actually
gets an account or IP banned.
"""
import re
import urllib.parse

import net

MANGADEX_API = "https://api.mangadex.org"
MANGADEX_UA = "NetScrollOffline/1.0 (personal offline archive; +https://mangadex.org)"

# WEBTOON language editions that exist as separate catalogs.
WEBTOON_LOCALES = {
    "en": "English", "es": "Español", "fr": "Français", "de": "Deutsch",
    "th": "ไทย", "id": "Bahasa Indonesia", "zh-hant": "繁體中文",
    "pt-br": "Português (BR)", "ja": "日本語",
}

# Content ratings to include for MangaDex; safe by default.
MANGADEX_RATINGS = ("safe", "suggestive")


class SourceError(Exception):
    """A source could not satisfy the request (network, empty result, bad id)."""


# ---------------------------------------------------------------- webtoon

def parse_webtoon_url(url: str) -> dict:
    """Extract language/genre/slug/title_no from a webtoons.com list/viewer URL."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    # The segment after the slug may be "list" or an episode path ending in
    # "/viewer" (which contains a slash, so it needs a non-slug-safe match).
    m = re.search(r"webtoons\.com/([a-z]{2}(?:-[a-z]+)?)/([a-z0-9-]+)/([^/]+)/"
                  r"(?:list|.*?viewer)", url)
    if not m:
        raise ValueError(
            "Could not parse that URL. It should look like "
            "https://www.webtoons.com/en/action/my-series-name/list?title_no=123"
        )
    lang, genre, slug = m.group(1), m.group(2), m.group(3)
    title_no = (q.get("title_no") or [None])[0]
    if not title_no:
        raise ValueError("The URL must contain a title_no parameter (?title_no=123).")
    return {"lang": lang, "genre": genre, "slug": slug, "title_no": title_no}


WEBTOON_GENRES = (
    "action", "comedy", "drama", "fantasy", "horror", "mystery", "romance",
    "scifi", "slice-of-life", "sports", "superhero", "thriller", "heartwarming",
)

# Titles that live only in one locale catalog (Spanish originals, German
# translations, etc.) are not reachable from the English list page. These
# browse URLs expose them.
def webtoon_genre_url(language: str, genre: str, page: int = 1) -> str:
    return f"https://www.webtoons.com/{language}/genres/{genre}?page={page}"


def webtoon_catalog(language: str, genre: str, pages: int = 1, cookie=None) -> list:
    """List titles in one locale's genre catalog.

    Returns ``[{'title_no','title','authors','url'}]`` for the given language
    edition. This is the supported way to add a series that only exists in,
    say, the Spanish or Thai catalog.
    """
    from bs4 import BeautifulSoup

    out, seen = [], set()
    for page in range(1, max(1, pages) + 1):
        url = webtoon_genre_url(language, genre, page)
        try:
            r = net.get(url, cookie=cookie, allow_status=(404,), attempts=2,
                        headers={"Referer": f"https://www.webtoons.com/{language}/"})
        except Exception:
            break
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        added = 0
        for a in soup.select("a[href*='list?title_no=']"):
            href = a.get("href") or ""
            m = re.search(r"title_no=(\d+)", href)
            if not m:
                continue
            title_no = m.group(1)
            if title_no in seen:
                continue
            title = (a.select_one(".subj") or a).get_text(" ", strip=True)
            if not title or len(title) > 120:
                continue
            seen.add(title_no)
            out.append({"title_no": title_no, "title": title,
                        "authors": "", "url": href if href.startswith("http")
                        else f"https://www.webtoons.com{href}"})
            added += 1
        if added == 0:
            break
    return out


def webtoon_alternates(parts: dict, cookie=None, session=None) -> list:
    """Report which other WEBTOON language editions list this exact URL.

    Note an important reality: WEBTOON redirects a title URL to the edition
    that actually owns it, so most "alternates" are the same English page.
    Use :func:`webtoon_catalog` to browse an edition's own catalogue; this
    function only answers "does this title exist there under that title_no".
    """
    from concurrent.futures import ThreadPoolExecutor

    candidates = [loc for loc in WEBTOON_LOCALES if loc != parts.get("lang")]

    def probe(loc):
        url = (f"https://www.webtoons.com/{loc}/{parts['genre']}/{parts['slug']}"
               f"/list?title_no={parts['title_no']}")
        try:
            r = net.get(url, cookie=cookie, session=session,
                        headers={"Referer": "https://www.webtoons.com/en/"},
                        allow_status=(404, 400), attempts=2)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        title = ""
        m = re.search(r"<title>([^<]{0,120})", r.text)
        if m:
            title = m.group(1).strip()
        # final URL tells us which edition really served the page
        served = re.search(r"webtoons\.com/([a-z]{2}(?:-[a-z]+)?)/", r.url or "")
        return {"language": loc, "url": url, "title": title,
                "served_by": served.group(1) if served else loc,
                "same_as_current": bool(served and served.group(1) == parts.get("lang"))}

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = [x for x in pool.map(probe, candidates) if x]
    return sorted(found, key=lambda x: x["language"])


# ---------------------------------------------------------------- mangadex

_UA = MANGADEX_UA


def _mdx_get(url, session=None, allow_status=()):
    """GET against the MangaDex API with its own descriptive user agent."""
    return net.get(url, session=session, timeout=30,
                   headers={"User-Agent": _UA, "Accept": "application/json"},
                   allow_status=allow_status)


def mangadex_search(title: str, limit: int = 5, languages=("en",)) -> list:
    """Search MangaDex for a title. Returns metadata dicts (no downloads)."""
    params = {
        "title": title,
        "limit": str(max(1, min(limit, 25))),
        "includes[]": "cover_art",
        "order[relevance]": "desc",
    }
    query = urllib.parse.urlencode(params)
    if languages:
        query += "".join(f"&availableTranslatedLanguage%5B%5D={l}" for l in languages)
    if MANGADEX_RATINGS:
        query += "".join(f"&contentRating%5B%5D={r}" for r in MANGADEX_RATINGS)
    r = _mdx_get(f"{MANGADEX_API}/manga?{query}")
    data = r.json()
    out = []
    for item in data.get("data") or []:
        attrs = item.get("attributes") or {}
        title_map = attrs.get("title") or {}
        cover = ""
        for rel in item.get("relationships") or []:
            if rel.get("type") == "cover_art":
                fn = (rel.get("attributes") or {}).get("fileName")
                if fn:
                    cover = f"https://uploads.mangadex.org/covers/{item['id']}/{fn}.256.jpg"
        out.append({
            "source": "mangadex",
            "id": item["id"],
            "title": title_map.get("en") or next(iter(title_map.values()), "") or "",
            "authors": ", ".join(
                (rel.get("attributes") or {}).get("name", "")
                for rel in item.get("relationships") or []
                if rel.get("type") == "author"),
            "description": ((attrs.get("description") or {}).get("en") or "")[:2000],
            "status": attrs.get("status") or "",
            "year": attrs.get("year"),
            "cover": cover,
            "original_language": attrs.get("originalLanguage") or "",
            "available_languages": attrs.get("availableTranslatedLanguages") or [],
        })
    return out


def mangadex_chapters(manga_id: str, language: str = "en", limit: int = 500) -> list:
    """List readable chapters for one manga, oldest first."""
    chapters, offset = [], 0
    while len(chapters) < limit:
        params = {
            "limit": "100",
            "offset": str(offset),
            "translatedLanguage[]": language,
            "order[chapter]": "asc",
            "includeExternalUrl": "0",
        }
        query = urllib.parse.urlencode(params)
        r = _mdx_get(f"{MANGADEX_API}/manga/{manga_id}/feed?{query}")
        data = r.json()
        batch = data.get("data") or []
        if not batch:
            break
        for ch in batch:
            attrs = ch.get("attributes") or {}
            if attrs.get("isUnavailable"):
                continue
            if attrs.get("externalUrl"):
                continue  # hosted elsewhere; MangaDex cannot serve pages
            no = attrs.get("chapter") or ""
            try:
                number = int(float(no))
            except (TypeError, ValueError):
                number = len(chapters) + 1
            chapters.append({
                "id": ch["id"],
                "episode_no": number,
                "title": (attrs.get("title") or "").strip(),
                "volume": attrs.get("volume") or "",
                "pages": attrs.get("pages") or 0,
                "language": attrs.get("translatedLanguage") or language,
                "group_id": next(
                    (rel.get("id") for rel in ch.get("relationships") or []
                     if rel.get("type") == "scanlation_group"), ""),
            })
        offset += 100
        if len(batch) < 100:
            break
    return chapters[:limit]


def mangadex_pages(chapter_id: str, data_saver: bool = False) -> tuple:
    """Resolve a chapter to page image URLs. Returns (urls, chapter_hash)."""
    r = _mdx_get(f"{MANGADEX_API}/at-home/server/{chapter_id}", allow_status=(404,))
    if r.status_code != 200:
        raise SourceError(f"chapter {chapter_id} is not served (404)")
    data = r.json()
    base = data.get("baseUrl") or ""
    chapter = data.get("chapter") or {}
    digest = chapter.get("hash") or ""
    files = (chapter.get("dataSaver") if data_saver else chapter.get("data")) or []
    mode = "data-saver" if data_saver else "data"
    urls = [f"{base}/{mode}/{digest}/{name}" for name in files]
    return urls, digest


def mangadex_report(base_url: str, chapter_hash: str, success: bool):
    """Tell MangaDex@Home whether a server worked (their documented courtesy API).

    Best-effort: never allowed to fail a download.
    """
    try:
        net.get(f"{base_url}/report", timeout=10, attempts=1,
                headers={"User-Agent": _UA},
                allow_status=(200, 204, 400, 404, 405))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- tapas

TAPAS = "https://tapas.io"

# Tapas' robots.txt allows /series/ and /episode/ for every agent; it only
# disallows /search, /rss/, /mature, /account/ and profile pages, which this
# module never requests.
# Tapas returns a *different* (JSON-escaped) document when the page is asked
# for with X-Requested-With, so HTML and JSON requests keep separate headers.
_TAPAS_HTML_HEADERS = {"User-Agent": MANGADEX_UA,
                       "Accept": "text/html,application/xhtml+xml"}
_TAPAS_JSON_HEADERS = {"User-Agent": MANGADEX_UA,
                       "Accept": "application/json, text/javascript",
                       "X-Requested-With": "XMLHttpRequest"}


def _tapas_get(url, session=None, allow_status=(), json=False):
    return net.get(url, session=session, timeout=30,
                   headers=_TAPAS_JSON_HEADERS if json else _TAPAS_HTML_HEADERS,
                   allow_status=allow_status)


def parse_tapas_url(url: str) -> dict:
    """Extract the series slug and numeric id from a Tapas series URL.

    Accepts ``https://tapas.io/series/<slug>`` (with or without ``/info``).
    The numeric id is resolved separately, because the slug alone is enough to
    load the page but the episode API needs the id.
    """
    m = re.search(r"tapas\.io/series/([A-Za-z0-9_\-]+)", url or "")
    if not m:
        raise ValueError(
            "Could not parse that Tapas URL. It should look like "
            "https://tapas.io/series/my-series-name")
    return {"slug": m.group(1)}


def tapas_resolve(series_url: str) -> dict:
    """Load a Tapas series page and pull its id, title, author and cover."""
    from bs4 import BeautifulSoup

    if not series_url.rstrip("/").endswith("/info"):
        series_url = series_url.rstrip("/") + "/info"
    r = _tapas_get(series_url, allow_status=(404,))
    if r.status_code != 200:
        raise SourceError(f"Tapas returned HTTP {r.status_code} for {series_url}")
    soup = BeautifulSoup(r.text, "html.parser")
    series_id = ""
    for el in soup.select("[data-series-id]"):
        # Values may arrive JSON-escaped (\"157924\") on some responses.
        val = re.sub(r"[^0-9]", "", el.get("data-series-id") or "")
        if val.isdigit():
            series_id = val
            break
    if not series_id:
        # The page also embeds it as JSON, e.g. "series_id":157924
        m = re.search(r'"series_id"\s*:\s*\\?"?(\d+)', r.text)
        if m:
            series_id = m.group(1)
    if not series_id:
        raise SourceError("Could not find the Tapas series id on that page.")

    title = ""
    for sel in ("h1.title", "h1", ".title"):
        el = soup.select_one(sel)
        if el:
            title = el.get_text(" ", strip=True)
            if title:
                break
    if not title:
        m = (re.search(r'property="og:title"\s+content="([^"]+)"', r.text) or
             re.search(r'name="title"\s+content="([^"]+)"', r.text))
        title = m.group(1) if m else "Untitled"
    title = re.sub(r"\s*\|\s*Tapas.*$", "", title).strip()
    desc_el = (soup.select_one("meta[property='og:description']") or
               soup.select_one("meta[name='description']"))
    desc = (desc_el.get("content") or "") if desc_el else ""
    cover_el = (soup.select_one("meta[property='og:image']") or
                soup.select_one("meta[itemprop='image']"))
    cover = (cover_el.get("content") or "") if cover_el else ""
    return {"series_id": series_id, "slug": parse_tapas_url(series_url)["slug"],
            "title": title, "description": desc, "cover": cover}


def tapas_episodes(series_id: str, max_pages: int = 60) -> list:
    """List every episode of a Tapas series, oldest first.

    Uses the same JSON endpoint the series page calls. Paywalled or locked
    episodes are included but flagged so the caller can report them honestly
    instead of trying to download something an account cannot read.
    """
    out, page = [], 1
    seen = set()
    while page <= max_pages:
        url = (f"{TAPAS}/series/{series_id}/episodes"
               f"?page={page}&sort=OLDEST&init_load=1")
        try:
            r = _tapas_get(url, allow_status=(400, 404), json=True)
        except Exception:
            break
        if r.status_code != 200:
            break
        try:
            payload = r.json().get("data") or {}
        except ValueError:
            break
        batch = payload.get("episodes") or []
        for ep in batch:
            ep_id = str(ep.get("id") or "")
            if not ep_id or ep_id in seen:
                continue
            seen.add(ep_id)
            out.append({
                "id": ep_id,
                "episode_no": int(ep.get("scene") or ep.get("pending_scene") or len(out) + 1),
                "title": ep.get("title") or "",
                "free": bool(ep.get("free")),
                "unlocked": bool(ep.get("unlocked")),
                "early_access": bool(ep.get("early_access")),
                "mature": bool(ep.get("mature")),
                "published": ep.get("publish_date") or "",
            })
        pagination = payload.get("pagination") or {}
        if not pagination.get("has_next") or not batch:
            break
        page += 1
    out.sort(key=lambda e: (e["episode_no"], e["id"]))
    return out


def tapas_pages(episode_id: str) -> list:
    """Resolve an episode's page image URLs.

    Tapas serves them as ``class="content__img"`` elements with ``data-src``.
    The URLs carry a signed, short-lived token, so they must be fetched soon
    after this call rather than cached.
    """
    url = f"{TAPAS}/episode/{episode_id}"
    r = _tapas_get(url, allow_status=(404, 403))
    if r.status_code != 200:
        raise SourceError(f"Tapas episode {episode_id} returned HTTP {r.status_code}")
    urls = re.findall(
        r'class="content__img[^"]*"[^>]*data-src="([^"]+)"', r.text)
    if not urls:
        # Locked / subscription-gated episodes render no page images at all.
        if "js-have-to-sign" in r.text or "subscribe" in r.text.lower():
            raise SourceError("episode is locked for this session")
        raise SourceError("no page images found (episode may be locked)")
    return [html_unescape(u) for u in urls]


def html_unescape(value: str) -> str:
    import html as _html
    return _html.unescape(value)


# ---------------------------------------------------------------- dispatch


def detect(url: str) -> dict:
    """Classify a pasted URL into ``{source, ...parts}``.

    Raises ValueError with a helpful message when the URL is not recognised.
    """
    u = (url or "").strip()
    if not u:
        raise ValueError("Paste a series URL first.")
    low = u.lower()
    if "webtoons.com" in low:
        parts = parse_webtoon_url(u)
        return {"source": "webtoon", "parts": parts}
    if "mangadex.org" in low:
        m = re.search(r"/title/([0-9a-fA-F-]{36})", u)
        if not m:
            raise ValueError(
                "That MangaDex link has no title id. Use a URL like "
                "https://mangadex.org/title/<uuid>")
        return {"source": "mangadex", "manga_id": m.group(1).lower()}
    if "tapas.io" in low:
        return {"source": "tapas", "parts": parse_tapas_url(u)}
    raise ValueError(
        "Unsupported site. Paste a webtoons.com series URL, a "
        "mangadex.org/title/<uuid> link, or a tapas.io/series/<name> link.")


def label(source: str) -> str:
    return {"webtoon": "WEBTOON", "mangadex": "MangaDex",
            "tapas": "Tapas"}.get(source, source)
