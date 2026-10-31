"""WEBTOON (webtoons.com) helpers for NetScrollOffline.

Two sources of truth we rely on:
  1) The web episode list page -> currently free/web-readable episodes.
  2) The web viewer page -> data-url list of full-resolution page images,
     which also exists for episodes your account can legitimately read
     (that's why Settings lets you paste your webtoons.com session cookie).

We deliberately do not touch Naver's private app APIs, third-party mirrors,
or any unlock/paywall bypass. The downloader only saves pages that are
actually readable in the web viewer.
"""
import os, re, time, requests, urllib.parse
from bs4 import BeautifulSoup
import db

VIEWER_IMG_RE = re.compile(r'class="_images"[^>]*data-url="([^"]+)"')

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _headers(cookie: str = None, referer: str = None) -> dict:
    h = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.webtoons.com/en/",
    }
    # Combined cookie: GDPR banner flag + user's webtoons.com session.
    cookie_value = "needGDPRsetVisible=false"
    if cookie:
        cookie_value = cookie.strip() + "; " + cookie_value
    h["Cookie"] = cookie_value
    if referer:
        h["Referer"] = referer
    return h


def _get(session: requests.Session, url: str, cookie: str = None) -> requests.Response:
    """GET with one polite retry on transient errors."""
    delay = float(db.connect().execute(
        "SELECT value FROM settings WHERE key='check_delay'").fetchone()[0])
    last_exc = None
    for attempt in range(3):
        try:
            r = session.get(url, headers=_headers(cookie), timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 + attempt * 3)
                continue
            return r
        except requests.RequestException as e:
            last_exc = e
            time.sleep(2 + attempt * 3)
    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


def base_url(parts: dict) -> str:
    return (
        "https://www.webtoons.com/{lang}/{genre}/{slug}"
        .format(**parts)
    )


def parse_series_url(url: str) -> dict:
    """Extract language/genre/slug/title_no from any list/viewer URL."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    m = re.search(r"webtoons\.com/([a-z]{2})/([a-z0-9-]+)/([^/]+)/(?:list|[^/]*viewer)", url)
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


def list_url(parts: dict) -> str:
    return f"{base_url(parts)}/list?title_no={parts['title_no']}"


def get_series_meta(parts: dict, cookie: str = None) -> dict:
    """Fetch title/description/cover/episode list. Only web-visible episodes returned."""
    s = requests.Session()
    url = list_url(parts)
    r = _get(s, url, cookie)
    if r.status_code != 200:
        raise RuntimeError(f"webtoons.com returned HTTP {r.status_code} for the series page.")
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    t = soup.find("meta", attrs={"property": "og:title"})
    title = t["content"].strip() if t and t.get("content") else None
    if not title:
        h1 = soup.find("h1", class_="subj")
        title = h1.get_text(strip=True) if h1 else "Untitled series"

    d = soup.find("meta", attrs={"property": "og:description"})
    desc = d["content"].strip() if d and d.get("content") else ""

    cover, kind = "", "thumb"
    detail = soup.select_one("div.detail_header")
    if detail and detail.get("style"):
        mv = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", detail["style"])
        if mv:
            cover, kind = mv.group(1), "banner"
    if not cover:
        t2 = soup.find("meta", attrs={"property": "og:image"})
        cover = t2["content"] if t2 and t2.get("content") else ""

    authors = ""
    author_div = soup.select_one(".author_area")
    if author_div:
        authors = author_div.get_text(" ", strip=True)

    episodes = []
    for li in soup.select("li._episodeItem"):
        no = li.get("data-episode-no")
        a = li.find("a", href=True)
        span = li.select_one("span.subj")
        if not (no and a):
            continue
        episodes.append({
            "episode_no": int(no),
            "title": span.get_text(strip=True) if span else li.get_text(" ", strip=True)[:120],
            "url": a["href"].split(";")[0],
        })

    status = ""
    day_info = soup.find("p", class_="day_info")
    if day_info:
        txt = day_info.get_text(" ", strip=True).upper()
        ico = day_info.find("span")
        ico_class = " ".join(ico.get("class", [])) if (ico and ico.get("class")) else ""
        if "txt_ico_completed" in ico_class:
            status = "COMPLETED"
        elif "hiatus" in txt or "BREAK" in txt:
            status = "HIATUS"
        elif "txt_ico_up" in ico_class or "every" in txt.lower():
            status = "ONGOING"

    return {"title": title, "description": desc, "cover": cover, "cover_kind": kind,
            "authors": authors, "status": status, "episodes": episodes}


def check_episode(episode_url: str, cookie: str = None) -> str:
    """Probe a viewer page and classify it: available | locked/unavailable."""
    s = requests.Session()
    r = _get(s, episode_url, cookie)
    if r.status_code != 200:
        return "unavailable"
    if '<div class="viewer_img' in r.text:
        return "available"
    if "Connect Error" in r.text:
        return "unavailable"
    return "unknown"


def search_immediate(keyword: str, cookie: str = None) -> list:
    """WEBTOON's own live search (used by the site's search box).

    Returns [{'titleNo', 'title', 'authors', 'genre', 'thumb'}...]
    """
    import json as _json
    s = requests.Session()
    url = f"https://www.webtoons.com/en/search/immediate?keyword={urllib.parse.quote_plus(keyword)}"
    r = _get(s, url, cookie)
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except _json.decoder.JSONDecodeError:
        return []
    res = data.get("result") or {}
    out = []
    for item in (res.get("searchedList") or []):
        out.append({
            "title_no": str(item.get("titleNo") or ""),
            "title": item.get("title") or "",
            "authors": ", ".join(item.get("authorNameList") or []) or
                       (item.get("authorName") or ""),
            "genre": item.get("representGenre") or "",
            "thumb": (item.get("imageDomain") or "") + (item.get("thumbnailMobile") or ""),
        })
    return out


def resolve_title_noURL(title_no: str) -> dict:
    """Discover the canonical series URL for a bare title number.

    Uses the webapp-legacy endpoint https://www.webtoons.com/episodeList?titleNo=N
    which 301-redirects to the canonical /{lang}/{genre}/{slug}/list URL.
    Returns {language, genre, slug, title_no} or raises RuntimeError.
    """
    s = requests.Session()
    r = _get(s, f"https://www.webtoons.com/episodeList?titleNo={title_no}")
    final = r.url
    m = re.search(r"webtoons\.com/([a-z]{2})/([a-z0-9-]+)/([^/]+)/list", final)
    if not m:
        raise RuntimeError(f"Could not resolve a URL for title_no {title_no}.")
    return {"language": m.group(1), "genre": m.group(2),
            "slug": m.group(3), "title_no": str(title_no)}


def estimate_series(parts: dict, cookie: str = None) -> dict:
    """Metadata + episode totals from the public list page(s).

    Returns {title, authors, description, cover, cover_kind, status,
             episodes, ep_total} where ep_total is the site-wide count.
    """
    meta = get_series_meta(parts, cookie)
    total = len(meta["episodes"])
    if meta["episodes"]:
        # pages are 10-per-page, newest first; find the last page number
        s = requests.Session()
        r = _get(s, list_url(parts), cookie)
        pages = [int(p) for p in
                 re.findall(r"[?&]page=(\d+)", r.text)]
        if pages:
            last = max(pages)
            if last > 1:
                r2 = _get(s, f"{list_url(parts)}&page={last}", cookie)
                soup2 = BeautifulSoup(r2.text, "html.parser")
                total = (last - 1) * 10 + len(soup2.select("li._episodeItem"))
        total = max(total, len(meta["episodes"]))
    meta["ep_total"] = max(total, meta.get("max_episode_no") or 0,
                           max((e["episode_no"] for e in meta["episodes"]), default=0))
    return meta


def download_episode(series, ep, cookie=None, progress_cb=None):
    """Download one episode. Returns (saved_paths, status).

    status: 'downloaded' | 'locked' | 'unavailable'
    """
    s = requests.Session()
    delay = float(db.connect().execute(
        "SELECT value FROM settings WHERE key='img_delay'").fetchone()[0])
    r = _get(s, ep["url"], cookie)
    if r.status_code == 200 and '<div class="viewer_img' not in r.text:
        return [], ("locked" if "Connect Error" not in r.text else "unavailable")
    urls = VIEWER_IMG_RE.findall(r.text)
    if not urls:
        return [], "unavailable"

    ep_dir = db.episode_dir(series["title_no"], ep["episode_no"])
    os.makedirs(ep_dir, exist_ok=True)
    saved = []
    for i, url in enumerate(urls):
        path = os.path.join(ep_dir, f"{i:03d}{os.path.splitext(urlparse_path(url))[1]}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            saved.append(path)
        else:
            rr = s.get(url, headers=_headers(cookie, referer=ep["url"]), timeout=60)
            rr.raise_for_status()
            with open(path, "wb") as f:
                f.write(rr.content)
            saved.append(path)
        if progress_cb:
            progress_cb(i + 1, len(urls))
        if i + 1 < len(urls):
            time.sleep(delay)
    return saved, "downloaded"


def urlparse_path(url: str) -> str:
    return urllib.parse.urlparse(url).path
