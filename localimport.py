"""Adopt downloads that already exist on disk into the library.

The library is a database: the Library page lists rows in ``series`` and
``episodes``. Downloads written by an older install (or by a copy of this app
whose ``data/`` folder was not carried over) are just folders on disk, so the
app shows an empty library even though the pages are all there.

This module walks a downloads directory, recognises the on-disk layout

    <root>/<title_no>/ep_0001/000.jpg

(and the older unpadded ``ep_1`` / bare ``12`` variants), validates that the
folders actually contain images, and rebuilds the matching ``series`` and
``episodes`` rows with ``status='downloaded'``.

It never deletes or moves anything, and it never downgrades a row that is
already marked downloaded.
"""
import os
import re
import time

import db
import scanner

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# ep_0001, ep_1, EP_12
_EP_DIR_RE = re.compile(r"^ep_?(\d+)$", re.IGNORECASE)
# a bare numeric folder such as 12
_BARE_DIR_RE = re.compile(r"^(\d+)$")


class ImportError_(Exception):
    """The requested folder cannot be imported (missing, unreadable)."""


def _images_in(path):
    """Return the image files in one episode folder, sorted."""
    out = []
    try:
        entries = os.scandir(path)
    except OSError:
        return out
    with entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name.lower().endswith(".part"):
                continue
            if not name.lower().endswith(IMAGE_EXTS):
                continue
            try:
                if entry.is_file() and entry.stat().st_size > 0:
                    out.append(entry.path)
            except OSError:
                continue
    out.sort()
    return out


def _episode_no(dirname):
    m = _EP_DIR_RE.match(dirname)
    if not m:
        m = _BARE_DIR_RE.match(dirname)
    return int(m.group(1)) if m else None


def _resolve_root(root):
    """Accept the app folder, a downloads folder, or one series folder."""
    root = os.path.abspath(os.path.expanduser(root or ""))
    if not os.path.isdir(root):
        raise ImportError_(f"Folder not found: {root}")
    # Pointed at an app folder that contains downloads/ — use that.
    nested = os.path.join(root, "downloads")
    if os.path.isdir(nested):
        root = nested
    return root


def _looks_like_single_series(root):
    """True when ``root`` itself holds episode folders (e.g. downloads/95).

    A numeric *series* key (``downloads/95``) looks exactly like a bare
    episode folder, so the test is whether one of those child folders holds
    images directly. ``downloads/95`` holds ``ep_0001/`` instead, which means
    ``95`` is a series and ``root`` is a downloads folder.
    """
    try:
        with os.scandir(root) as it:
            children = [e for e in it if e.is_dir()]
    except OSError:
        return False
    for entry in children:
        if _episode_no(entry.name) is None:
            continue
        if _images_in(entry.path):
            return True
    return False


def scan(root, limit_series=None):
    """Find every series/episode folder with real images under ``root``.

    Returns ``[{'key', 'episodes': [{'no', 'dir', 'images', 'partial'}]}]``.
    Folders with no usable images are skipped entirely.
    """
    root = _resolve_root(root)

    if _looks_like_single_series(root):
        series_dirs = [root]
    else:
        try:
            with os.scandir(root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise ImportError_(f"Cannot read {root}: {exc}") from exc
        series_dirs = [e.path for e in entries if e.is_dir() and not e.name.startswith(".")]

    found = []
    for series_dir in series_dirs:
        key = os.path.basename(series_dir.rstrip(os.sep))
        episodes = []
        try:
            with os.scandir(series_dir) as it:
                ep_entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in ep_entries:
            if not entry.is_dir():
                continue
            no = _episode_no(entry.name)
            if no is None:
                continue
            images = _images_in(entry.path)
            if not images:
                continue
            try:
                partial = any(
                    n.lower().endswith(".part")
                    for n in os.listdir(entry.path))
            except OSError:
                partial = False
            episodes.append({
                "no": no,
                "dir": entry.path,
                "images": images,
                "partial": partial,
                "bytes": sum(os.path.getsize(p) for p in images),
            })
        if not episodes:
            continue
        episodes.sort(key=lambda e: e["no"])
        found.append({"key": key, "path": series_dir, "episodes": episodes})
        if limit_series and len(found) >= limit_series:
            break
    return found


# ---------------------------------------------------------------- metadata

def _infer_source(key):
    low = key.lower()
    if low.startswith("md_"):
        return "mangadex", key[3:]
    if low.startswith("tapas_"):
        return "tapas", key[len("tapas_"):]
    if key.isdigit():
        return "webtoon", key
    return "webtoon", key


def _placeholder_title(key, source):
    if source == "mangadex":
        return f"MangaDex title {key[3:][:8]}"
    if source == "tapas":
        return f"Tapas series {key[len('tapas_'):]}"
    if key.isdigit():
        return f"WEBTOON #{key}"
    return key


def resolve_metadata(key, lookup=True):
    """Best-effort title/cover/language for an on-disk series folder.

    ``lookup=False`` (or being offline) leaves a readable placeholder instead
    of failing the import. Returns a dict with ``resolved`` set to whether the
    network lookup actually succeeded.
    """
    source, ref = _infer_source(key)
    meta = {
        "source": source, "source_ref": ref,
        "language": "en", "genre": "", "slug": key,
        "title": _placeholder_title(key, source),
        "authors": "", "description": "", "cover": "", "cover_kind": "thumb",
        "status": "", "resolved": False,
    }
    if not lookup:
        return meta

    try:
        if source == "webtoon" and key.isdigit():
            import webtoon
            parts = webtoon.resolve_title_noURL(key)
            info = webtoon.get_series_meta(parts, full=False)
            meta.update({
                "language": parts["language"], "genre": parts["genre"],
                "slug": parts["slug"],
                "title": info["title"] or meta["title"],
                "authors": info["authors"], "description": info["description"],
                "cover": info["cover"], "cover_kind": info["cover_kind"],
                "status": info["status"], "resolved": True,
            })
        elif source == "mangadex":
            import sources
            r = sources._mdx_get(
                f"{sources.MANGADEX_API}/manga/{ref}"
                "?includes[]=cover_art&includes[]=author")
            data = (r.json() or {}).get("data") or {}
            attrs = data.get("attributes") or {}
            titles = attrs.get("title") or {}
            cover = ""
            for rel in data.get("relationships") or []:
                if rel.get("type") == "cover_art":
                    fn = (rel.get("attributes") or {}).get("fileName")
                    if fn:
                        cover = (f"https://uploads.mangadex.org/covers/{ref}/{fn}.512.jpg")
            meta.update({
                "title": titles.get("en") or next(iter(titles.values()), "")
                         or meta["title"],
                "authors": ", ".join(
                    (rel.get("attributes") or {}).get("name", "")
                    for rel in data.get("relationships") or []
                    if rel.get("type") == "author"),
                "description": ((attrs.get("description") or {}).get("en") or "")[:4000],
                "cover": cover, "cover_kind": "banner",
                "status": (attrs.get("status") or "").upper(),
                "resolved": True,
            })
        elif source == "tapas":
            import sources
            # The folder only stores the numeric id; the episode API accepts it.
            eps = sources.tapas_episodes(ref, max_pages=1)
            if eps:
                meta["resolved"] = True
    except Exception:
        # Offline, deleted title, rate limit: keep the placeholder and move on.
        meta["resolved"] = False
    return meta


# ---------------------------------------------------------------- adopt

def _series_url(meta, key):
    if meta["source"] == "webtoon" and meta["genre"]:
        return (f"https://www.webtoons.com/{meta['language']}/{meta['genre']}/"
                f"{meta['slug']}/list?title_no={key}")
    if meta["source"] == "mangadex":
        return f"https://mangadex.org/title/{meta['source_ref']}"
    if meta["source"] == "tapas":
        return f"https://tapas.io/series/{meta['source_ref']}"
    return ""


def adopt(found, lookup=True, progress=None):
    """Write scanned folders into the database. Returns a summary dict."""
    stats = {"series": 0, "new_series": 0, "episodes": 0, "new_episodes": 0,
             "bytes": 0, "partial": 0, "resolved": 0}
    conn = db.connect()
    try:
        for index, item in enumerate(found, 1):
            key = item["key"]
            meta = resolve_metadata(key, lookup=lookup)
            if meta["resolved"]:
                stats["resolved"] += 1
            now = db.now()
            with conn:
                existing = conn.execute(
                    "SELECT id, title FROM series WHERE title_no=?", (key,)).fetchone()
                if existing:
                    series_id = existing["id"]
                    if meta["resolved"]:
                        conn.execute(
                            "UPDATE series SET title=?, language=?, genre=?, slug=?,"
                            " authors=?, description=?, cover=?, cover_kind=?,"
                            " series_status=coalesce(nullif(?,''), series_status),"
                            " source=?, source_ref=? WHERE id=?",
                            (meta["title"], meta["language"], meta["genre"],
                             meta["slug"], meta["authors"], meta["description"],
                             meta["cover"], meta["cover_kind"], meta["status"],
                             meta["source"], meta["source_ref"], series_id))
                    else:
                        conn.execute(
                            "UPDATE series SET source=coalesce(nullif(source,''),?),"
                            " source_ref=coalesce(nullif(source_ref,''),?) WHERE id=?",
                            (meta["source"], meta["source_ref"], series_id))
                else:
                    cur = conn.execute(
                        "INSERT INTO series (title_no, language, genre, slug, title,"
                        " authors, description, cover, cover_kind, series_status,"
                        " max_episode_no, last_checked, source, source_ref)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, meta["language"], meta["genre"], meta["slug"],
                         meta["title"], meta["authors"], meta["description"],
                         meta["cover"], meta["cover_kind"], meta["status"],
                         0, now, meta["source"], meta["source_ref"]))
                    series_id = cur.lastrowid
                    stats["new_series"] += 1
                stats["series"] += 1

                url = _series_url(meta, key)
                for ep in item["episodes"]:
                    status = "unknown" if ep["partial"] else "downloaded"
                    error = ("incomplete download (.part files present)"
                             if ep["partial"] else "")
                    stamp = time.strftime(
                        "%Y-%m-%dT%H:%M:%S",
                        time.localtime(max(os.path.getmtime(p) for p in ep["images"])))
                    before = conn.execute(
                        "SELECT id FROM episodes WHERE series_id=? AND episode_no=?",
                        (series_id, ep["no"])).fetchone()
                    conn.execute(
                        "INSERT INTO episodes (series_id, episode_no, title, url, known,"
                        " status, error, image_count, downloaded_at)"
                        " VALUES (?,?,?,?,0,?,?,?,?)"
                        " ON CONFLICT(series_id, episode_no) DO UPDATE SET"
                        " status=CASE WHEN excluded.status='downloaded'"
                        "             THEN 'downloaded' ELSE episodes.status END,"
                        " image_count=MAX(episodes.image_count, excluded.image_count),"
                        " downloaded_at=CASE WHEN excluded.downloaded_at!=''"
                        "                    THEN excluded.downloaded_at"
                        "                    ELSE episodes.downloaded_at END,"
                        " url=CASE WHEN coalesce(episodes.url,'')=''"
                        "          THEN excluded.url ELSE episodes.url END",
                        (series_id, ep["no"], f"Episode {ep['no']}", url,
                         status, error, len(ep["images"]), stamp))
                    stats["episodes"] += 1
                    if not before:
                        stats["new_episodes"] += 1
                    if ep["partial"]:
                        stats["partial"] += 1
                    stats["bytes"] += ep["bytes"]

                conn.execute(
                    "UPDATE series SET max_episode_no=coalesce((SELECT MAX(episode_no)"
                    " FROM episodes WHERE series_id=?),0) WHERE id=?",
                    (series_id, series_id))
            if progress:
                progress(index, len(found), meta["title"])
    finally:
        conn.close()
    return stats
