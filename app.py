"""NetScrollOffline - a small local web app for owning your webtoon library."""
import os, re, time, threading, json, urllib.parse, difflib
import zipfile, tempfile, html, shutil
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, Response, send_file, send_from_directory, abort)
import db
import webtoon
import sources
import net
import scanner
from requests.exceptions import RequestException

app = Flask(__name__)

# ---------------------------------------------------------------- session key
_key_path = os.path.join(os.path.dirname(db.DB_PATH), "secret_key")
os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
if not os.path.exists(_key_path):
    with open(_key_path, "wb") as f:
        f.write(os.urandom(32))
with open(_key_path, "rb") as f:
    app.secret_key = f.read()

# ---------------------------------------------------------------- background jobs
# A small in-memory job queue with an adjustable number of concurrency "slots",
# cancellation, pausing and automatic retry of failed jobs. Job records stay in
# memory until they are cleared from the Queue page.
JOBS = {}
JOBS_LOCK = threading.RLock()
DISPATCH = threading.Condition(JOBS_LOCK)
QUEUE = {"slots": 2, "paused": False, "auto_retry": True, "max_attempts": 3}
QUEUE_HIDDEN = ("fn", "cancel")
RETRY_DELAY = 20.0


class JobCancelled(Exception):
    """Raised from inside a job body when the user cancels that job."""


class JobError(Exception):
    """Raised from inside a job body to mark the job as failed (and retryable)."""


def get_setting(key, default=""):
    conn = db.connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db.connect()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.close()


def get_cookie():
    return (get_setting("webtoon_cookie") or "").strip()


def current_profile():
    """Return the profile row for this session (defaults to first profile)."""
    pid = session.get("profile_id")
    conn = db.connect()
    row = None
    if pid:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone()
        if row:
            session["profile_id"] = row["id"]
    conn.close()
    return row


_QUEUE_SETTINGS_LOADED = False


def _load_queue_settings():
    """Restore queue preferences saved in the settings table."""
    global _QUEUE_SETTINGS_LOADED
    if _QUEUE_SETTINGS_LOADED:
        return
    _QUEUE_SETTINGS_LOADED = True
    try:
        slots = int(get_setting("queue_slots", "2") or 2)
    except ValueError:
        slots = 2
    try:
        attempts = int(get_setting("queue_max_attempts", "3") or 3)
    except ValueError:
        attempts = 3
    auto = (get_setting("queue_auto_retry", "1") or "1").lower() in ("1", "true", "on", "yes")
    with JOBS_LOCK:
        QUEUE["slots"] = max(1, min(8, slots))
        QUEUE["max_attempts"] = max(1, min(20, attempts))
        QUEUE["auto_retry"] = auto


def _save_queue_settings():
    set_setting("queue_slots", str(QUEUE["slots"]))
    set_setting("queue_max_attempts", str(QUEUE["max_attempts"]))
    set_setting("queue_auto_retry", "1" if QUEUE["auto_retry"] else "0")


def _series_label(series_id):
    """Human-readable series name for job descriptions."""
    if not series_id:
        return ""
    conn = db.connect()
    row = conn.execute("SELECT title FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    return (row["title"] if row else f"series {series_id}").strip()


def _new_job(desc, fn=None, kind="task", series_id=None, params=None,
             max_attempts=None, auto_retry=None):
    """Create a job record and queue it. The dispatcher starts it when a slot frees up."""
    _load_queue_settings()
    with JOBS_LOCK:
        if max_attempts is None:
            max_attempts = QUEUE["max_attempts"]
        if auto_retry is None:
            auto_retry = QUEUE["auto_retry"]
        job_id = f"job{(max((int(k[3:]) for k in JOBS if k[3:].isdigit()), default=0) + 1)}"
        JOBS[job_id] = {
            "id": job_id, "kind": kind, "desc": desc, "series_id": series_id,
            "params": params or {}, "fn": fn,
            "status": "queued", "done": 0, "total": 0,
            "message": "Queued", "messages": [], "error": "", "failed": 0,
            "attempts": 0, "max_attempts": max(1, int(max_attempts)),
            "auto_retry": bool(auto_retry), "retry_at": 0.0,
            "retry_delay": RETRY_DELAY,
            "cancel": False,
            "created_at": time.time(), "started_at": None, "finished_at": None,
        }
        DISPATCH.notify_all()
    return job_id


def _job_status(job_id):
    """Public view of a job (never exposes the callable, which JSON can't encode)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return _job_dict(job) if job else None


def _update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)
        DISPATCH.notify_all()


def _job_cancel_requested(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancel"))


def _check_cancel(job_id, message="Cancelled"):
    """Called from inside a job body so long loops stop promptly."""
    if _job_cancel_requested(job_id):
        raise JobCancelled(message)


def _sleep_or_cancel(job_id, seconds):
    """Sleep in small slices so a cancel request takes effect quickly."""
    if seconds <= 0:
        _check_cancel(job_id)
        return
    deadline = time.time() + seconds
    while time.time() < deadline:
        _check_cancel(job_id)
        time.sleep(min(0.25, max(0.0, deadline - time.time())))
    _check_cancel(job_id)


def _job_dict(job, include_private=False):
    data = {k: v for k, v in job.items() if include_private or k not in QUEUE_HIDDEN}
    data.pop("retry_delay", None)
    return data


def _queue_snapshot():
    with JOBS_LOCK:
        jobs = [_job_dict(j) for j in JOBS.values()]
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
        return {
            "jobs": jobs,
            "slots": QUEUE["slots"],
            "running": running,
            "paused": QUEUE["paused"],
            "auto_retry": QUEUE["auto_retry"],
            "max_attempts": QUEUE["max_attempts"],
            "queued": sum(1 for j in JOBS.values() if j["status"] == "queued"),
        }


def _finalize_job(job_id, status, error=""):
    """Settle a finished job, scheduling an automatic retry when appropriate."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["finished_at"] = time.time()
        job["error"] = error or job.get("error", "")
        retryable = job["kind"] in ("download", "refresh", "check", "export", "watchlist")
        partial_failure = job["kind"] == "download" and job["failed"] > 0
        if status == "done" and partial_failure:
            status = "error"
            job["error"] = job["error"] or f"{job['failed']} episode(s) failed"
            job["message"] = f"Finished with {job['failed']} failed episode(s)"
        wants_retry = job["auto_retry"] and retryable and not job["cancel"] and (
            status == "error" or partial_failure)
        if wants_retry and job["attempts"] < job["max_attempts"]:
            # Back off a little each time, but never wait more than 5 minutes.
            delay = min(job["retry_delay"] * job["attempts"], 300.0)
            job["status"] = "queued"
            job["retry_at"] = time.time() + delay
            job["failed"] = 0
            job["done"] = 0
            job["message"] = (f"Auto-retry in {int(delay)}s "
                              f"(attempt {job['attempts'] + 1}/{job['max_attempts']})")
            job["finished_at"] = None
        else:
            job["status"] = status
            if status == "done":
                job["error"] = ""
                if not job["message"] or job["message"].startswith("Starting"):
                    job["message"] = "Finished"
            elif status == "cancelled":
                job["message"] = "Cancelled"
            elif status == "error":
                job["message"] = job["error"] or "Failed"
        DISPATCH.notify_all()


def _start_job(job_id):
    """Run one job in its own thread (called by the dispatcher under a free slot)."""
    def runner():
        with JOBS_LOCK:
            raw = JOBS.get(job_id)
            fn = raw.get("fn") if raw else None
        try:
            if fn is not None:
                fn()
        except JobCancelled:
            _finalize_job(job_id, "cancelled", "Cancelled")
        except JobError as e:
            _finalize_job(job_id, "error", str(e)[:400])
        except Exception as e:  # never let a job thread die silently
            import traceback
            traceback.print_exc()
            _finalize_job(job_id, "error", str(e)[:400] or e.__class__.__name__)
        else:
            st = _job_status(job_id) or {}
            if st.get("status") == "running":
                if _job_cancel_requested(job_id):
                    _finalize_job(job_id, "cancelled", "Cancelled")
                else:
                    _finalize_job(job_id, "done")

    threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()


def _dispatcher():
    """Starts queued jobs while respecting the slot limit, pause flag and retry delays."""
    while True:
        with DISPATCH:
            while True:
                if QUEUE["paused"]:
                    DISPATCH.wait(0.5)
                    continue
                running = sum(1 for j in JOBS.values() if j["status"] == "running")
                slots = QUEUE["slots"]
                now = time.time()
                ready = None
                soonest = None
                for job in sorted(JOBS.values(), key=lambda j: j["created_at"]):
                    if job["status"] != "queued":
                        continue
                    if job["retry_at"] and job["retry_at"] > now:
                        soonest = job["retry_at"] if soonest is None else min(soonest, job["retry_at"])
                        continue
                    ready = job
                    break
                if ready is not None and running < slots:
                    ready["status"] = "running"
                    ready["attempts"] += 1
                    ready["started_at"] = ready["started_at"] or now
                    ready["retry_at"] = 0.0
                    ready["message"] = f"Starting (attempt {ready['attempts']}/{ready['max_attempts']})"
                    job_id = ready["id"]
                    break
                timeout = 0.5
                if soonest is not None:
                    timeout = max(0.05, min(timeout, soonest - now))
                DISPATCH.wait(timeout)
        _start_job(job_id)


threading.Thread(target=_dispatcher, daemon=True, name="job-dispatcher").start()


def _delay_ge_zero(name):
    try:
        v = float(get_setting(name, "0"))
    except ValueError:
        v = 0
    return max(v, 0.0)


def _setting_bool(name, default=False):
    raw = (get_setting(name, "1" if default else "0") or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


def refresh_series_episodes(series_id, progress_cb=None):
    """Recrawl the episode list for this series' source and upsert rows."""
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    if not series:
        raise RuntimeError("series not found")
    if (series["source"] or "webtoon") == "mangadex":
        return _refresh_mangadex_series(series, progress_cb)
    if (series["source"] or "webtoon") == "tapas":
        return _refresh_tapas_series(series, progress_cb)
    parts = {"lang": series["language"], "genre": series["genre"],
             "slug": series["slug"], "title_no": series["title_no"]}
    if not parts["genre"]:
        conn = db.connect()
        row = conn.execute(
            "SELECT url FROM episodes WHERE series_id=? ORDER BY episode_no LIMIT 1",
            (series_id,)).fetchone()
        conn.close()
        if row:
            m = re.search(r"/([a-z]{2})/([a-z0-9-]+)/([^/]+)/.+viewer", row["url"])
            if m:
                parts.update({"lang": m.group(1), "genre": m.group(2), "slug": m.group(3)})
        if not parts["genre"]:
            raise RuntimeError("Not enough info to rebuild the series URL; use a full"
                               " list or viewer URL when adding the series.")

    meta = webtoon.get_series_meta(parts, cookie=get_cookie(), full=True,
                                   progress_cb=progress_cb)
    conn = db.connect()
    count = 0
    with conn:
        conn.execute(
            "UPDATE series SET title=?, description=?, cover=coalesce(nullif(?,''),cover),"
            " cover_kind=?, authors=?, series_status=coalesce(nullif(?,''),series_status),"
            " genre=?, last_checked=? WHERE id=?",
            (meta["title"], meta["description"], meta["cover"], meta["cover_kind"],
             meta["authors"], meta["status"], parts["genre"], db.now(), series_id))
        for ep in meta["episodes"]:
            conn.execute(
                "INSERT INTO episodes (series_id, episode_no, title, url, known, status)"
                " VALUES (?,?,?,?,1,'unknown')"
                " ON CONFLICT(series_id, episode_no) DO UPDATE SET known=1,"
                " title=excluded.title, url=excluded.url",
                (series_id, ep["episode_no"], ep["title"], ep["url"]))
            conn.execute(
                "UPDATE series SET max_episode_no=max(max_episode_no, ?) WHERE id=?",
                (ep["episode_no"], series_id))
            count += 1
    conn.close()
    return count


def _refresh_mangadex_series(series, progress_cb=None):
    """Re-read a MangaDex title's metadata and English chapter list."""
    manga_id = series["source_ref"] or series["slug"]
    r = sources._mdx_get(f"{sources.MANGADEX_API}/manga/{manga_id}"
                         "?includes[]=cover_art&includes[]=author")
    data = (r.json() or {}).get("data") or {}
    attrs = data.get("attributes") or {}
    title_map = attrs.get("title") or {}
    if progress_cb:
        progress_cb(1, 0)
    chapters = sources.mangadex_chapters(manga_id, language="en")
    conn = db.connect()
    count = 0
    try:
        with conn:
            cover = ""
            for rel in data.get("relationships") or []:
                if rel.get("type") == "cover_art":
                    fn = (rel.get("attributes") or {}).get("fileName")
                    if fn:
                        cover = f"https://uploads.mangadex.org/covers/{manga_id}/{fn}.512.jpg"
            conn.execute(
                "UPDATE series SET title=coalesce(nullif(?,''),title),"
                " description=coalesce(nullif(?,''),description),"
                " cover=coalesce(nullif(?,''),cover),"
                " series_status=coalesce(nullif(?,''),series_status), last_checked=?"
                " WHERE id=?",
                (title_map.get("en") or next(iter(title_map.values()), ""),
                 ((attrs.get("description") or {}).get("en") or "")[:4000],
                 cover, (attrs.get("status") or "").upper(), db.now(), series["id"]))
            for ch in chapters:
                conn.execute(
                    "INSERT INTO episodes (series_id, episode_no, title, url, known, status)"
                    " VALUES (?,?,?,?,1,'unknown') ON CONFLICT(series_id, episode_no)"
                    " DO UPDATE SET known=1, title=excluded.title, url=excluded.url",
                    (series["id"], ch["episode_no"],
                     ch["title"] or f"Chapter {ch['episode_no']}", ch["id"]))
                count += 1
                if progress_cb and count % 25 == 0:
                    progress_cb(1, count)
            conn.execute(
                "UPDATE series SET max_episode_no=coalesce((SELECT MAX(episode_no)"
                " FROM episodes WHERE series_id=?),0) WHERE id=?",
                (series["id"], series["id"]))
    finally:
        conn.close()
    return count


def _refresh_tapas_series(series, progress_cb=None):
    """Re-read a Tapas series' episode list."""
    series_id = series["source_ref"] or ""
    if not series_id:
        raise RuntimeError("Tapas series has no stored id; re-add it from its URL.")
    episodes = sources.tapas_episodes(series_id)
    if progress_cb:
        progress_cb(1, len(episodes))
    conn = db.connect()
    try:
        with conn:
            for ep in episodes:
                readable = ep["free"] or ep["unlocked"]
                conn.execute(
                    "INSERT INTO episodes (series_id, episode_no, title, url, known, status,"
                    " error) VALUES (?,?,?,?,1,?,?) ON CONFLICT(series_id, episode_no)"
                    " DO UPDATE SET known=1, title=excluded.title, url=excluded.url",
                    (series["id"], ep["episode_no"],
                     ep["title"] or f"Episode {ep['episode_no']}", ep["id"],
                     "unknown" if readable else "locked",
                     "" if readable else "paid or early-access episode"))
            conn.execute(
                "UPDATE series SET last_checked=?, max_episode_no=coalesce("
                "(SELECT MAX(episode_no) FROM episodes WHERE series_id=?),0) WHERE id=?",
                (db.now(), series["id"], series["id"]))
    finally:
        conn.close()
    return len(episodes)


def do_refresh(job_id, series_id):
    """Background crawl of the full web episode list."""
    _update_job(job_id, total=0, message="Fetching episode list…")

    def progress(page, found):
        _check_cancel(job_id)
        _update_job(job_id, message=f"Page {page} · {found} episodes found")

    count = refresh_series_episodes(series_id, progress_cb=progress)
    _update_job(job_id, done=count, total=count,
                message=f"{count} episodes in the list")


def do_check_range(job_id, series_id, start, end):
    cookie = get_cookie()
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE series_id=? AND episode_no BETWEEN ? AND ?"
        " AND status!='downloaded' ORDER BY episode_no", (series_id, start, end)).fetchall()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    _update_job(job_id, total=len(rows))
    delay = _delay_ge_zero("check_delay")
    failed = 0
    for i, ep in enumerate(rows):
        _check_cancel(job_id)
        status = webtoon.check_episode(ep["url"], cookie)
        if status == "unavailable":
            failed += 1
        mapped = {"available": "available", "unavailable": "unknown"}.get(status, "unknown")
        conn = db.connect()
        with conn:
            conn.execute("UPDATE episodes SET status=?, error='' WHERE id=?",
                         (mapped, ep["id"]))
        conn.close()
        _update_job(job_id, done=i + 1, failed=failed,
                    message=f"Ep {ep['episode_no']}: {status}")
        if i + 1 < len(rows):
            _sleep_or_cancel(job_id, delay)


def _series_known_count(conn, series_id):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE series_id=?", (series_id,)).fetchone()["c"]


def _series_looks_sparse(conn, series_id, max_no):
    """True when the local episode list has big holes (only the newest page was
    ever fetched). Fresh series get ~9 rows while max_episode_no is much higher."""
    if max_no <= 20:
        return False
    have = _series_known_count(conn, series_id)
    return have < max_no * 0.6


def _download_chapter_images(job_id, series, ep, series_title_no, cookie=None):
    """Fetch one MangaDex chapter's pages. Returns (saved_paths, status)."""
    scan = (_setting_bool("scan_downloads"))
    scan_av = (_setting_bool("scan_antivirus"))
    delay = _delay_ge_zero("img_delay")
    try:
        urls, _digest = sources.mangadex_pages(ep["url"])
    except Exception as e:
        return [], f"error: {e}"
    if not urls:
        return [], "unavailable"
    ep_dir = db.episode_dir(series_title_no, ep["episode_no"])
    os.makedirs(ep_dir, exist_ok=True)
    saved = []
    for n, url in enumerate(urls, 1):
        _check_cancel(job_id)
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1] or ".jpg"
        path = os.path.join(ep_dir, f"{n - 1:03d}{ext}")

        def fetch(u):
            return net.get(u, session=None, timeout=60,
                           headers={"User-Agent": "NetScrollOffline/1.0"})

        if os.path.exists(path) and os.path.getsize(path) > 0:
            saved.append(path)
        elif scan or scan_av:
            ok, reason = scanner.download_to(url, path, fetch, scan=scan_av)
            if not ok:
                return saved, f"corrupt: {reason}"
            saved.append(path)
        else:
            data = fetch(url).content
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            saved.append(path)
        if len(urls) > 1 and delay > 0 and n < len(urls):
            time.sleep(delay)
    return saved, "downloaded"


def _download_tapas_episode(job_id, series, ep, series_title_no):
    """Fetch one Tapas episode's pages. Returns (saved_paths, status)."""
    scan = _setting_bool("scan_downloads")
    scan_av = _setting_bool("scan_antivirus")
    delay = _delay_ge_zero("img_delay")
    try:
        urls = sources.tapas_pages(ep["url"])
    except Exception as e:
        msg = str(e)
        if "locked" in msg.lower():
            return [], "locked"
        return [], f"error: {msg}"
    if not urls:
        return [], "unavailable"
    ep_dir = db.episode_dir(series_title_no, ep["episode_no"])
    os.makedirs(ep_dir, exist_ok=True)
    saved = []
    for n, url in enumerate(urls, 1):
        _check_cancel(job_id)
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1] or ".jpg"
        path = os.path.join(ep_dir, f"{n - 1:03d}{ext}")

        def fetch(u):
            return net.get(u, timeout=60, referer=f"https://tapas.io/episode/{ep['url']}",
                           headers={"User-Agent": sources.MANGADEX_UA})

        if os.path.exists(path) and os.path.getsize(path) > 0:
            saved.append(path)
        elif scan or scan_av:
            ok, reason = scanner.download_to(url, path, fetch, scan=scan_av)
            if not ok:
                return saved, f"corrupt: {reason}"
            saved.append(path)
        else:
            data = fetch(url).content
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            saved.append(path)
        if n < len(urls) and delay > 0:
            time.sleep(delay)
    return saved, "downloaded"


def do_download(job_id, series_id, start, end, full=False):
    cookie = get_cookie()
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    sparse = bool(series) and _series_looks_sparse(conn, series_id,
                                                   series["max_episode_no"] or 0)
    conn.close()
    if full or (sparse and start <= 1):
        # "Download the whole series": always recrawl first so episodes added
        # since the last refresh are included, and a series that only ever
        # stored the newest page gets its back catalogue. On a sparse series
        # we also do this for a partial range starting at 1.
        _update_job(job_id, message="Refreshing the full episode list…")
        refresh_series_episodes(
            series_id,
            progress_cb=lambda page, found: _update_job(
                job_id, message=f"Episode list page {page} · {found} episodes"))
        if full:
            conn = db.connect()
            row = conn.execute("SELECT max_episode_no FROM series WHERE id=?",
                               (series_id,)).fetchone()
            conn.close()
            end = max(end, row["max_episode_no"] if row else end)
    conn = db.connect()
    rows = conn.execute(
        "SELECT e.*, s.title_no, s.slug, s.language, s.id AS sid FROM episodes e"
        " JOIN series s ON s.id=e.series_id"
        " WHERE e.series_id=? AND e.episode_no BETWEEN ? AND ? AND e.status!='downloaded'"
        " ORDER BY e.episode_no", (series_id, start, end)).fetchall()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    _update_job(job_id, total=len(rows))
    msgs = []
    failed = 0
    for i, ep in enumerate(rows):
        _check_cancel(job_id)
        try:
            if (series["source"] or "webtoon") == "mangadex":
                saved, status = _download_chapter_images(
                    job_id, series, ep, series["title_no"], cookie)
            elif (series["source"] or "webtoon") == "tapas":
                saved, status = _download_tapas_episode(
                    job_id, series, ep, series["title_no"])
            else:
                saved, status = webtoon.download_episode(series, ep, cookie)
        except RequestException as e:
            status, saved = "error", []
            failed += 1
            conn = db.connect()
            with conn:
                conn.execute("UPDATE episodes SET status='error', error=? WHERE id=?",
                             (str(e)[:300], ep["id"]))
            conn.close()
            msgs.append(f"Ep {ep['episode_no']}: ERROR {str(e)[:120]}")
            _update_job(job_id, done=i + 1, failed=failed, message=msgs[-1])
            continue
        conn = db.connect()
        with conn:
            if status == "locked":
                conn.execute("UPDATE episodes SET status='locked', error=? WHERE id=?",
                             ("not readable on the web viewer for this account", ep["id"]))
            elif status == "unavailable":
                conn.execute("UPDATE episodes SET status='unknown', error=? WHERE id=?",
                             ("web viewer answered an error (often app-only titles)", ep["id"]))
            elif status.startswith("corrupt") or status.startswith("error"):
                # A failed integrity check is a real failure: mark it so the
                # job's retry logic and the queue UI both surface it.
                conn.execute("UPDATE episodes SET status='error', error=? WHERE id=?",
                             (status[:300], ep["id"]))
                failed += 1
            else:
                conn.execute(
                    "UPDATE episodes SET status=?, image_count=?, downloaded_at=?, error=''"
                    " WHERE id=?", (status, len(saved), db.now(), ep["id"]))
        conn.close()
        if status == "unavailable":
            failed += 1
        msgs.append(f"Ep {ep['episode_no']}: {status} ({len(saved)} pages)")
        _update_job(job_id, done=i + 1, failed=failed, message=msgs[-1])
        _sleep_or_cancel(job_id, _delay_ge_zero("check_delay"))
    write_phoenix_records()
    if failed:
        _update_job(job_id, message=f"{failed} of {len(rows)} episode(s) failed")


# ---------------------------------------------------------------- export
EXPORTS_DIR = os.path.join(db.DATA_BASE, "exports")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def _safe_name(name, fallback="untitled"):
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:110] or fallback).rstrip(". ")


def _comic_info(series, ep, page_count):
    def e(v):
        return html.escape(str(v if v is not None else ""))

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<ComicInfo xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" "
        "xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\">\n"
        f"  <Title>{e(ep['title'] or 'Episode ' + str(ep['episode_no']))}</Title>\n"
        f"  <Series>{e(series['title'])}</Series>\n"
        f"  <Number>{e(ep['episode_no'])}</Number>\n"
        f"  <Writer>{e(series['authors'])}</Writer>\n"
        f"  <Summary>{e(series['description'][:4000])}</Summary>\n"
        f"  <Genre>{e(series['genre_tags'])}</Genre>\n"
        f"  <LanguageISO>{e(series['language'])}</LanguageISO>\n"
        f"  <PageCount>{e(page_count)}</PageCount>\n"
        f"  <Web>{e(ep['url'])}</Web>\n"
        "</ComicInfo>\n"
    )


def _episode_images(series, ep_no):
    d = db.episode_dir(series["title_no"], ep_no)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.lower().endswith(IMG_EXTS) and not f.startswith(".")]


def do_export(job_id, series_id=None, fmt="cbz"):
    """Bundle downloaded episodes into one downloadable ZIP.

    fmt='cbz'  -> one .cbz comic archive per episode (Komga/Kavita friendly)
    fmt='raw'  -> plain image folders
    series_id=None exports every downloaded episode in the library.
    """
    conn = db.connect()
    if series_id:
        all_series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchall()
    else:
        all_series = conn.execute("SELECT * FROM series ORDER BY title COLLATE NOCASE").fetchall()
    plan = []
    for s in all_series:
        eps = conn.execute(
            "SELECT * FROM episodes WHERE series_id=? AND status='downloaded'"
            " ORDER BY episode_no", (s["id"],)).fetchall()
        for ep in eps:
            imgs = _episode_images(s, ep["episode_no"])
            if imgs:
                plan.append((s, ep, imgs))
    conn.close()

    _update_job(job_id, total=len(plan))
    if not plan:
        raise JobError("Nothing downloaded yet — run a download first.")

    os.makedirs(EXPORTS_DIR, exist_ok=True)
    # Prune archives from earlier exports (older than a day) so repeated
    # exports don't fill the disk.
    cutoff = time.time() - 24 * 3600
    for f in os.listdir(EXPORTS_DIR):
        fp = os.path.join(EXPORTS_DIR, f)
        try:
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if series_id and len(all_series) == 1:
        label = _safe_name(all_series[0]["title"], "series")
    elif all_series:
        label = "library"
    else:
        label = "library"
    token = f"exp{int(time.time() * 1000)}"
    out_path = os.path.join(EXPORTS_DIR, f"{label}-{stamp}-{token}.zip")

    manifest = {"format": fmt, "generated": db.now(),
                "episodes": []}
    used_dirs = {}
    try:
        _write_export_zip(out_path, token, plan, fmt, manifest, used_dirs, job_id)
    except Exception:
        # Never leave a half-written archive behind for the download route.
        EXPORTS.pop(token, None)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
        raise

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    _update_job(job_id, url=f"/api/export/{token}", size_mb=round(size_mb, 1),
                filename=os.path.basename(out_path),
                message=f"Export ready — {len(plan)} episode(s), {size_mb:.1f} MB")
    EXPORTS[token] = out_path


def _write_export_zip(out_path, token, plan, fmt, manifest, used_dirs, job_id):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for i, (s, ep, imgs) in enumerate(plan):
            _check_cancel(job_id)
            if s["id"] not in used_dirs:
                name = _safe_name(s["title"], s["title_no"])
                if name in used_dirs.values():
                    name = f"{name} [{s['title_no']}]"
                used_dirs[s["id"]] = name
            series_dir = used_dirs[s["id"]]
            ep_no = ep["episode_no"]
            ep_title = _safe_name(ep["title"] or f"Episode {ep_no}", f"Episode {ep_no}")
            base = f"{series_dir}/{ep_no:04d} - {ep_title}"
            if fmt == "cbz":
                tmp_cbz = os.path.join(EXPORTS_DIR, f".tmp-{token}-{i:06d}.cbz")
                try:
                    with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_STORED) as c:
                        for img in imgs:
                            c.write(img, os.path.basename(img))
                        c.writestr("ComicInfo.xml", _comic_info(s, ep, len(imgs)))
                    z.write(tmp_cbz, base + ".cbz")
                finally:
                    if os.path.exists(tmp_cbz):
                        os.remove(tmp_cbz)
            else:
                for img in imgs:
                    z.write(img, f"{base}/{os.path.basename(img)}")
                z.writestr(f"{base}/ComicInfo.xml", _comic_info(s, ep, len(imgs)))
            manifest["episodes"].append({
                "series": s["title"], "title_no": s["title_no"],
                "episode": ep_no, "title": ep["title"],
                "pages": len(imgs), "url": ep["url"],
                "path": base + (".cbz" if fmt == "cbz" else "/"),
            })
            _update_job(job_id, done=i + 1,
                        message=f"{s['title']} — ep {ep_no} ({len(imgs)} pages)")

        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr("README.txt", (
            "NetScrollOffline export\n"
            f"Format: {fmt}  ({'one .cbz per episode' if fmt == 'cbz' else 'image folders'})\n"
            f"Episodes: {len(plan)}\n"
            f"Generated: {manifest['generated']}\n\n"
            "Drop the folders/.cbz files into Komga, Kavita, Calibre, or any comic "
            "reader. manifest.json lists every episode and its source URL.\n"
        ))


EXPORTS = {}


# ---------------------------------------------------------------- routes

@app.context_processor
def inject_profile():
    profile = current_profile()
    conn = db.connect()
    profiles = conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    conn.close()
    return dict(profile=profile, profiles=profiles)


@app.after_request
def sw_scope_header(resp):
    """Allow the service worker to control all pages, not just /static/."""
    if resp.mimetype == "text/javascript" and request.path == "/static/sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/")
def library():
    profile = current_profile()
    conn = db.connect()
    series_rows = conn.execute("""
        SELECT s.*,
          (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.known=1) AS known_count,
          (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.status='downloaded') AS downloaded_count,
          (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.status='locked') AS locked_count,
          p.episode_no AS last_ep, p.scroll_frac AS last_frac
        FROM series s
        LEFT JOIN progress p ON p.series_id=s.id AND p.profile_id=?
        ORDER BY s.title COLLATE NOCASE""", (profile["id"],)).fetchall()
    conn.close()
    return render_template("library.html", series=series_rows,
                           locales=sources.WEBTOON_LOCALES,
                           genres=sources.WEBTOON_GENRES)


@app.post("/api/profile/switch")
def api_profile_switch():
    pid = request.form.get("profile_id") or request.json.get("profile_id")
    conn = db.connect()
    if conn.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone():
        session["profile_id"] = int(pid)
    conn.close()
    return redirect(request.form.get("next") or url_for("library"))


@app.post("/api/profile/create")
def api_profile_create():
    name = (request.form.get("name") or "").strip()[:40]
    if name:
        conn = db.connect()
        with conn:
            conn.execute("INSERT OR IGNORE INTO profiles (name) VALUES (?)", (name,))
            row = conn.execute("SELECT id FROM profiles WHERE name=?", (name,)).fetchone()
        conn.close()
        session["profile_id"] = row["id"]
    return redirect(url_for("library"))


@app.post("/api/series/add")
def api_series_add():
    url = (request.form.get("url") or "").strip()
    try:
        target = sources.detect(url)
    except ValueError as e:
        return render_template("error.html", message=str(e)), 400
    if target["source"] == "mangadex":
        return _add_mangadex_series(target["manga_id"])
    if target["source"] == "tapas":
        return _add_tapas_series(target["parts"]["slug"])
    parts = target["parts"]
    conn = db.connect()
    dup = conn.execute("SELECT id FROM series WHERE title_no=? AND source='webtoon'",
                       (parts["title_no"],)).fetchone()
    conn.close()
    if dup:
        return redirect(url_for("series_page", series_id=dup["id"]))

    # discover the genre by probing for the first page load of the list (genre in url)
    # try the url right away
    meta = None
    try:
        meta = webtoon.get_series_meta(parts, cookie=get_cookie())
    except Exception:
        # series list root may lack genre segment; try common originals path
        pass
    conn = db.connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO series (title_no, language, genre, slug, title, authors, description,"
            " cover, cover_kind, series_status, max_episode_no, last_checked, source, source_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (parts["title_no"], parts["lang"], parts["genre"], parts["slug"],
             meta["title"] if meta else "Untitled (needs refresh)",
             meta["authors"] if meta else "", meta["description"] if meta else "",
             meta["cover"] if meta else "", meta["cover_kind"] if meta else "thumb",
             meta["status"] if meta else "", 0, db.now(), "webtoon", parts["title_no"]))
        series_id = cur.lastrowid
        if meta:
            for ep in meta["episodes"]:
                conn.execute(
                    "INSERT INTO episodes (series_id, episode_no, title, url, known, status)"
                    " VALUES (?,?,?,?,1,'unknown')",
                    (series_id, ep["episode_no"], ep["title"], ep["url"]))
                conn.execute("UPDATE series SET max_episode_no=max(max_episode_no,?) WHERE id=?",
                             (ep["episode_no"], series_id))
    conn.close()
    return redirect(url_for("series_page", series_id=series_id))


def _add_mangadex_series(manga_id):
    """Import a MangaDex title and its English chapter list."""
    try:
        r = sources._mdx_get(f"{sources.MANGADEX_API}/manga/{manga_id}"
                             "?includes[]=cover_art&includes[]=author")
        data = (r.json() or {}).get("data") or {}
    except Exception as e:
        return render_template("error.html", message=f"MangaDex lookup failed: {e}"), 502
    attrs = data.get("attributes") or {}
    title_map = attrs.get("title") or {}
    title = title_map.get("en") or next(iter(title_map.values()), "") or "Untitled"
    authors = ", ".join((rel.get("attributes") or {}).get("name", "")
                        for rel in data.get("relationships") or []
                        if rel.get("type") == "author")
    desc = ((attrs.get("description") or {}).get("en") or "")[:4000]
    cover = ""
    for rel in data.get("relationships") or []:
        if rel.get("type") == "cover_art":
            fn = (rel.get("attributes") or {}).get("fileName")
            if fn:
                cover = f"https://uploads.mangadex.org/covers/{manga_id}/{fn}.512.jpg"

    conn = db.connect()
    dup = conn.execute("SELECT id FROM series WHERE source='mangadex' AND source_ref=?",
                       (manga_id,)).fetchone()
    conn.close()
    if dup:
        return redirect(url_for("series_page", series_id=dup["id"]))

    try:
        chapters = sources.mangadex_chapters(manga_id, language="en")
    except Exception as e:
        return render_template("error.html",
                               message=f"MangaDex chapter list failed: {e}"), 502

    conn = db.connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO series (title_no, language, genre, slug, title, authors, description,"
            " cover, cover_kind, series_status, max_episode_no, last_checked, source, source_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"md_{manga_id}", "en", "", manga_id, title, authors, desc,
             cover, "banner", (attrs.get("status") or "").upper(), 0, db.now(),
             "mangadex", manga_id))
        series_id = cur.lastrowid
        for ch in chapters:
            conn.execute(
                "INSERT INTO episodes (series_id, episode_no, title, url, known, status)"
                " VALUES (?,?,?,?,1,'unknown') ON CONFLICT(series_id, episode_no)"
                " DO UPDATE SET known=1, title=excluded.title, url=excluded.url",
                (series_id, ch["episode_no"],
                 ch["title"] or f"Chapter {ch['episode_no']}", ch["id"]))
        conn.execute("UPDATE series SET max_episode_no=coalesce((SELECT MAX(episode_no)"
                     " FROM episodes WHERE series_id=?),0) WHERE id=?", (series_id, series_id))
    conn.close()
    return redirect(url_for("series_page", series_id=series_id))


def _add_tapas_series(slug):
    """Import a Tapas series and its episode list."""
    try:
        info = sources.tapas_resolve(f"https://tapas.io/series/{slug}")
        episodes = sources.tapas_episodes(info["series_id"])
    except Exception as e:
        return render_template("error.html", message=f"Tapas lookup failed: {e}"), 502
    if not episodes:
        return render_template("error.html",
                               message="That Tapas series has no readable episodes."), 404

    conn = db.connect()
    dup = conn.execute("SELECT id FROM series WHERE source='tapas' AND source_ref=?",
                       (info["series_id"],)).fetchone()
    conn.close()
    if dup:
        return redirect(url_for("series_page", series_id=dup["id"]))

    conn = db.connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO series (title_no, language, genre, slug, title, authors,"
                " description, cover, cover_kind, series_status, max_episode_no,"
                " last_checked, source, source_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"tapas_{info['series_id']}", "en", "", info["slug"], info["title"],
                 "", info["description"], info["cover"], "banner", "", 0, db.now(),
                 "tapas", info["series_id"]))
            series_id = cur.lastrowid
            for ep in episodes:
                # Episodes the session cannot read are stored as locked so the
                # UI is honest instead of retrying them as network failures.
                readable = ep["free"] or ep["unlocked"]
                status = "unknown" if readable else "locked"
                err = "" if readable else "paid or early-access episode"
                conn.execute(
                    "INSERT INTO episodes (series_id, episode_no, title, url, known, status,"
                    " error) VALUES (?,?,?,?,1,?,?) ON CONFLICT(series_id, episode_no)"
                    " DO UPDATE SET known=1, title=excluded.title, url=excluded.url",
                    (series_id, ep["episode_no"], ep["title"] or
                     f"Episode {ep['episode_no']}", ep["id"], status, err))
            conn.execute("UPDATE series SET max_episode_no=coalesce((SELECT MAX(episode_no)"
                         " FROM episodes WHERE series_id=?),0) WHERE id=?",
                         (series_id, series_id))
    finally:
        conn.close()
    return redirect(url_for("series_page", series_id=series_id))


@app.route("/series/<int:series_id>")
def series_page(series_id):
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not series:
        abort(404)
    eps = conn.execute(
        "SELECT * FROM episodes WHERE series_id=? ORDER BY episode_no", (series_id,)).fetchall()
    known = conn.execute("SELECT COUNT(*) AS c FROM episodes WHERE series_id=? AND known=1",
                         (series_id,)).fetchone()["c"]
    profile = current_profile()
    prog = {p["episode_no"]: p for p in conn.execute(
        "SELECT * FROM progress WHERE series_id=? AND profile_id=?",
        (series_id, profile["id"])).fetchall()}
    sparse = _series_looks_sparse(conn, series_id, series["max_episode_no"] or 0)
    conn.close()
    return render_template("series.html", s=series, eps=eps, known=known, prog=prog,
                           sparse=sparse)


@app.post("/api/series/<int:series_id>/refresh")
def api_series_refresh(series_id):
    conn = db.connect()
    series = conn.execute("SELECT id FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    if not series:
        return jsonify(error="Series not found."), 404
    job_id = _new_job(f"Refresh episodes — {_series_label(series_id)}",
                      kind="refresh", series_id=series_id, max_attempts=2,
                      fn=lambda: do_refresh(job_id, series_id))
    return jsonify(ok=True, job=job_id)


@app.post("/api/series/<int:series_id>/manual_add")
def api_series_manual_add(series_id):
    start = int(request.form.get("start", 0) or 0)
    end = int(request.form.get("end", 0) or 0)
    pattern = (request.form.get("slug") or "ep-{n}").strip()
    if end < start or start < 1 or end - start > 500:
        return jsonify(error="Range looks wrong."), 400
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    known = conn.execute(
        "SELECT url FROM episodes WHERE series_id=? AND known=1 ORDER BY episode_no LIMIT 1",
        (series_id,)).fetchone()
    conn.close()
    if not series or not known:
        return jsonify(error="Refresh the series first so we know its web address format."), 400
    src = known["url"]
    base = re.sub(r"^(https://www\.webtoons\.com/[a-z]{2}/[a-z0-9-]+)/[^/]+/.+",
                  r"\1", src)
    conn = db.connect()
    n = 0
    with conn:
        for ep_no in range(start, end + 1):
            url = f"{base}/{pattern.format(n=ep_no)}/viewer?title_no={series['title_no']}&episode_no={ep_no}"
            conn.execute(
                "INSERT INTO episodes (series_id, episode_no, title, url, known, status)"
                " VALUES (?,?,?,?,0,'unknown')"
                " ON CONFLICT(series_id, episode_no) DO UPDATE SET url=excluded.url",
                (series_id, ep_no, "", url))
            conn.execute("UPDATE series SET max_episode_no=max(max_episode_no,?) WHERE id=?",
                         (ep_no, series_id))
            n += 1
    conn.close()
    return jsonify(ok=True, added=n)


@app.post("/api/series/<int:series_id>/delete")
def api_series_delete(series_id):
    import shutil
    conn = db.connect()
    series = conn.execute("SELECT title_no FROM series WHERE id=?", (series_id,)).fetchone()
    with conn:
        conn.execute("DELETE FROM episodes WHERE series_id=?", (series_id,))
        conn.execute("DELETE FROM progress WHERE series_id=?", (series_id,))
        conn.execute("DELETE FROM series WHERE id=?", (series_id,))
    conn.close()
    if series:
        shutil.rmtree(os.path.join(db.DOWNLOADS_DIR, series["title_no"]),
                      ignore_errors=True)
    return redirect(url_for("library"))


@app.post("/api/series/<int:series_id>/check")
def api_series_check(series_id):
    start = int(request.form.get("start", 1) or 1)
    end = int(request.form.get("end", 0) or 0)
    conn = db.connect()
    row = conn.execute("SELECT max_episode_no FROM series WHERE id=?",
                       (series_id,)).fetchone()
    conn.close()
    if not end:
        end = row["max_episode_no"] if row else start
    job_id = _new_job(f"Check {_series_label(series_id)} · episodes {start}-{end}",
                      kind="check", series_id=series_id,
                      fn=lambda: do_check_range(job_id, series_id, start, end))
    return jsonify(ok=True, job=job_id)


@app.post("/api/series/<int:series_id>/meta")
def api_series_meta(series_id):
    kind = (request.form.get("kind") or "").strip()
    tags = (request.form.get("genre_tags") or "").strip()[:200]
    status = (request.form.get("series_status") or "").strip().upper()
    if kind not in ("webtoon", "manga"):
        kind = "webtoon"
    if status and status not in ("COMPLETED", "ONGOING", "HIATUS"):
        status = ""
    conn = db.connect()
    with conn:
        conn.execute("UPDATE series SET kind=?, genre_tags=?, series_status="
                     "coalesce(nullif(?,''),series_status) WHERE id=?",
                     (kind, tags, status, series_id))
    conn.close()
    write_phoenix_records()
    return redirect(request.form.get("next") or url_for("series_page", series_id=series_id))


@app.post("/api/download")
def api_download():
    sid = int(request.form.get("series_id") or 0)
    start = int(request.form.get("start", 1) or 1)
    end = int(request.form.get("end", 0) or 0)
    full = request.form.get("full") in ("1", "true", "on")
    res = _queue_series_download(sid, start=start, end=end, full=full)
    if not res.get("ok"):
        return jsonify(error=res.get("error") or "Unknown series."), 404
    return jsonify(ok=True, job=res["job"], skipped=res.get("skipped", ""))


def _queue_series_download(series_id, start=1, end=0, full=True):
    """Queue one whole-series download, skipping series that are already in line.

    Shared by the single-series button and the library's bulk queue so both
    behave identically, including deduplication of duplicate requests.
    """
    conn = db.connect()
    row = conn.execute("SELECT max_episode_no FROM series WHERE id=?",
                       (series_id,)).fetchone()
    conn.close()
    if not row:
        return {"series_id": series_id, "ok": False, "error": "Unknown series."}
    if not end:
        end = row["max_episode_no"]
    with JOBS_LOCK:
        for job in JOBS.values():
            if (full and job["kind"] == "download" and job["series_id"] == series_id
                    and job["status"] in ("queued", "running")
                    and job["params"].get("full")):
                return {"series_id": series_id, "ok": True, "job": job["id"],
                        "skipped": "already queued"}
    job_id = _new_job(
        (f"Download all episodes — {_series_label(series_id)}" if full
         else f"Download {_series_label(series_id)} · episodes {start}-{end}"),
        kind="download", series_id=series_id,
        params={"start": start, "end": end, "full": full},
        fn=lambda: do_download(job_id, series_id, start, end, full=full))
    return {"series_id": series_id, "ok": True, "job": job_id}


@app.post("/api/queue/bulk")
def api_queue_bulk():
    """Queue whole-series downloads for many series at once (library selection)."""
    raw = request.form.get("series_ids") or ""
    if not raw and request.is_json:
        ids = (request.json or {}).get("series_ids") or []
        raw = ",".join(str(i) for i in ids)
    wanted = []
    for part in str(raw).replace(" ", ",").split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        sid = int(part)
        if sid not in wanted:
            wanted.append(sid)
    if not wanted:
        return jsonify(error="Pick at least one series."), 400
    if len(wanted) > 200:
        return jsonify(error="Too many series in one request (max 200)."), 400
    results = [_queue_series_download(sid) for sid in wanted]
    ok = sum(1 for r in results if r.get("ok"))
    skipped = sum(1 for r in results if r.get("skipped"))
    return jsonify(ok=True, queued=ok - skipped, skipped=skipped,
                   failed=len(results) - ok, results=results)


@app.post("/api/export")
def api_export():
    sid = request.form.get("series_id") or None
    try:
        sid = int(sid) if sid else None
    except (TypeError, ValueError):
        return jsonify(error="Bad series id."), 400
    fmt = request.form.get("format", "cbz")
    if fmt not in ("cbz", "raw"):
        fmt = "cbz"
    if sid:
        conn = db.connect()
        ok = conn.execute("SELECT 1 FROM series WHERE id=?", (sid,)).fetchone()
        conn.close()
        if not ok:
            return jsonify(error="Unknown series."), 404
    desc = (f"Export {_series_label(sid)} as {fmt.upper()}" if sid
            else f"Export whole library as {fmt.upper()}")
    job_id = _new_job(desc, kind="export", series_id=sid, params={"fmt": fmt},
                      fn=lambda: do_export(job_id, sid, fmt))
    return jsonify(ok=True, job=job_id)


@app.get("/api/export/<token>")
def api_export_download(token):
    if not re.fullmatch(r"exp\d+", token):
        abort(404)
    path = EXPORTS.get(token)
    if path is None or not os.path.isfile(path):
        # Survive a server restart: recover the archive by its token in the name.
        path = None
        if os.path.isdir(EXPORTS_DIR):
            for f in os.listdir(EXPORTS_DIR):
                if token in f and f.endswith(".zip"):
                    cand = os.path.join(EXPORTS_DIR, f)
                    if os.path.isfile(cand):
                        path = cand
                        break
        if path is None:
            abort(404)
    return send_from_directory(EXPORTS_DIR, os.path.basename(str(path)),
                               as_attachment=True)


@app.get("/api/job/<job_id>")
def api_job(job_id):
    return jsonify(_job_status(job_id) or {})


# ---------------------------------------------------------------- queue routes

@app.route("/queue")
def queue_page():
    _load_queue_settings()
    return render_template("queue.html")


@app.get("/api/queue")
def api_queue():
    _load_queue_settings()
    snap = _queue_snapshot()
    snap["host_limits"] = net.stats()
    return jsonify(snap)


@app.get("/api/limits")
def api_limits():
    """Current per-host request budget, and the configured pacing delays."""
    return jsonify(hosts=net.stats(),
                   check_delay=_delay_ge_zero("check_delay"),
                   img_delay=_delay_ge_zero("img_delay"),
                   scanning={"structure": _setting_bool("scan_downloads"),
                             "antivirus": _setting_bool("scan_antivirus"),
                             "antivirus_available": scanner.antivirus_available()})


@app.post("/api/limits/host")
def api_limit_set_host():
    """Override one host's request budget (advanced; persisted in memory only)."""
    host = (request.form.get("host") or "").strip()
    if host not in net.HOST_LIMITS:
        return jsonify(error="Unknown host."), 404
    try:
        rps = float(request.form.get("rps") or 0)
    except (TypeError, ValueError):
        return jsonify(error="rps must be a number."), 400
    if rps <= 0:
        return jsonify(error="rps must be greater than zero."), 400
    burst = request.form.get("burst")
    inflight = request.form.get("inflight")
    net.set_host_limit(host, rps,
                       int(burst) if burst and burst.isdigit() else None,
                       int(inflight) if inflight and inflight.isdigit() else None)
    return jsonify(ok=True, hosts=net.stats())


@app.get("/api/sources")
def api_sources():
    """Which sources are available and which locales can be added."""
    return jsonify(
        sources=[{"id": "webtoon", "label": "WEBTOON",
                  "notes": "webtoons.com web reader, including other language editions"},
                 {"id": "mangadex", "label": "MangaDex",
                  "notes": "official JSON API; English scanlations and non-WEBTOON titles"},
                 {"id": "tapas", "label": "Tapas",
                  "notes": "tapas.io web reader; robots.txt allows /series/ and /episode/"}],
        webtoon_locales=sources.WEBTOON_LOCALES,
        antivirus=scanner.antivirus_available(),
    )


@app.post("/api/sources/alternates")
def api_source_alternates():
    """List other WEBTOON locale editions for a pasted series URL."""
    url = (request.form.get("url") or "").strip()
    try:
        parts = webtoon.parse_series_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(alternates=sources.webtoon_alternates(parts, cookie=get_cookie()),
                   current=parts["lang"])


@app.get("/api/sources/catalog")
def api_source_catalog():
    """Browse one WEBTOON language edition's genre catalogue."""
    lang = (request.args.get("language") or "en").strip()
    genre = (request.args.get("genre") or "romance").strip()
    try:
        pages = max(1, min(10, int(request.args.get("pages") or 1)))
    except ValueError:
        pages = 1
    if lang not in sources.WEBTOON_LOCALES:
        return jsonify(error="Unknown language edition."), 400
    if genre not in sources.WEBTOON_GENRES:
        return jsonify(error="Unknown genre."), 400
    try:
        titles = sources.webtoon_catalog(lang, genre, pages=pages, cookie=get_cookie())
    except Exception as e:
        return jsonify(error=f"Could not browse that catalogue: {e}"), 502
    return jsonify(language=lang, genre=genre, titles=titles)


@app.post("/api/sources/add")
def api_source_add_by_id():
    """Add a series from a bare WEBTOON title_no (any locale edition)."""
    title_no = (request.form.get("title_no") or "").strip()
    if not title_no.isdigit():
        return jsonify(error="A numeric title_no is required."), 400
    try:
        parts = webtoon.resolve_title_noURL(title_no)
    except Exception as e:
        return jsonify(error=f"Could not resolve title_no {title_no}: {e}"), 502
    url = (f"https://www.webtoons.com/{parts['language']}/{parts['genre']}/"
           f"{parts['slug']}/list?title_no={title_no}")
    return jsonify(ok=True, url=url, language=parts["language"])


@app.post("/api/sources/mangadex/search")
def api_mangadex_search():
    term = (request.form.get("q") or "").strip()
    if len(term) < 2:
        return jsonify(error="Type at least two characters."), 400
    try:
        found = sources.mangadex_search(term, limit=8)
    except Exception as e:
        return jsonify(error=f"MangaDex search failed: {e}"), 502
    return jsonify(results=found)


@app.post("/api/queue/slots")
def api_queue_slots():
    raw = request.form.get("slots")
    if raw is None and request.is_json:
        raw = (request.json or {}).get("slots")
    try:
        slots = int(raw or 0)
    except (TypeError, ValueError):
        return jsonify(error="Slots must be a number."), 400
    if slots < 1:
        return jsonify(error="Slots must be a number."), 400
    slots = max(1, min(8, slots))
    with JOBS_LOCK:
        QUEUE["slots"] = slots
        DISPATCH.notify_all()
        _save_queue_settings()
    return jsonify(ok=True, slots=slots)


@app.post("/api/queue/pause")
def api_queue_pause():
    with JOBS_LOCK:
        QUEUE["paused"] = True
    return jsonify(ok=True, paused=True)


@app.post("/api/queue/resume")
def api_queue_resume():
    with JOBS_LOCK:
        QUEUE["paused"] = False
        DISPATCH.notify_all()
    return jsonify(ok=True, paused=False)


def _retry_job(job_id, reset_attempts=True):
    """Put a finished/failed job back in the queue and return its record."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        if job["status"] == "running":
            return job
        job["cancel"] = False
        job["status"] = "queued"
        job["retry_at"] = 0.0
        job["finished_at"] = None
        job["error"] = ""
        job["failed"] = 0
        job["done"] = 0
        job["total"] = 0
        job["message"] = "Queued (manual retry)"
        if reset_attempts:
            job["attempts"] = 0
        DISPATCH.notify_all()
        return dict(job)


@app.post("/api/queue/retry/<job_id>")
def api_queue_retry(job_id):
    # "Retry now" on a job that is already waiting for its auto-retry keeps its
    # attempt counter so it cannot loop forever.
    waiting = (_job_status(job_id) or {}).get("status") == "queued"
    job = _retry_job(job_id, reset_attempts=not waiting)
    if job is None:
        return jsonify(error="Unknown job."), 404
    if job["status"] == "running":
        return jsonify(error="Job is already running."), 409
    return jsonify(ok=True)


@app.post("/api/queue/cancel/<job_id>")
def api_queue_cancel(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Unknown job."), 404
        job["cancel"] = True
        if job["status"] in ("queued", "retry"):
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            job["message"] = "Cancelled before starting"
        elif job["status"] == "running":
            job["message"] = "Cancelling…"
        DISPATCH.notify_all()
    return jsonify(ok=True)


@app.post("/api/queue/remove/<job_id>")
def api_queue_remove(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Unknown job."), 404
        if job["status"] == "running":
            return jsonify(error="Cancel the job before removing it."), 409
        JOBS.pop(job_id, None)
    return jsonify(ok=True)


@app.post("/api/queue/clear")
def api_queue_clear():
    """Remove every job that is not queued or running."""
    with JOBS_LOCK:
        for job_id in [k for k, j in JOBS.items()
                       if j["status"] in ("done", "error", "cancelled")]:
            JOBS.pop(job_id, None)
    return jsonify(ok=True)


@app.post("/api/queue/retry-failed")
def api_queue_retry_failed():
    with JOBS_LOCK:
        ids = [k for k, j in JOBS.items() if j["status"] == "error"]
    for job_id in ids:
        _retry_job(job_id)
    return jsonify(ok=True, retried=len(ids))


@app.post("/api/queue/cancel-queued")
def api_queue_cancel_queued():
    with JOBS_LOCK:
        n = 0
        for job in JOBS.values():
            if job["status"] == "queued":
                job["cancel"] = True
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
                job["message"] = "Cancelled"
                n += 1
        DISPATCH.notify_all()
    return jsonify(ok=True, cancelled=n)


@app.post("/api/queue/auto-retry")
def api_queue_auto_retry():
    raw = (request.form.get("enabled") or "").lower()
    enabled = raw in ("1", "true", "on", "yes")
    attempts = request.form.get("max_attempts")
    max_attempts = None
    if attempts:
        try:
            max_attempts = max(1, min(20, int(attempts)))
        except (TypeError, ValueError):
            max_attempts = None
    with JOBS_LOCK:
        QUEUE["auto_retry"] = enabled
        if max_attempts is not None:
            QUEUE["max_attempts"] = max_attempts
        for job in JOBS.values():
            if job["status"] in ("queued", "running"):
                job["auto_retry"] = enabled
                if max_attempts is not None:
                    job["max_attempts"] = max_attempts
        _save_queue_settings()
    return jsonify(ok=True, auto_retry=enabled,
                   max_attempts=QUEUE["max_attempts"])


@app.get("/api/ep/<int:series_id>/<int:ep_no>/images")
def api_ep_images(series_id, ep_no):
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE series_id=? AND episode_no=?",
                      (series_id, ep_no)).fetchone()
    conn.close()
    if not series or not ep:
        return jsonify(error="not_found"), 404
    ep_dir = db.episode_dir(series["title_no"], ep_no)
    if os.path.isdir(ep_dir):
        files = sorted(f for f in os.listdir(ep_dir) if not f.startswith("."))
        if files:
            return jsonify(images=[f"/media/{series['title_no']}/ep_{ep_no:04d}/{f}"
                                   for f in files])
    if ep["status"] == "locked":
        return jsonify(error="locked", message=ep["error"] or
                       "This episode is not readable on the web viewer for your account.")
    remote = ep["url"]
    host_ok = urllib.parse.urlparse(remote).hostname or ""
    if not host_ok.endswith("webtoons.com"):
        return jsonify(error="bad_url"), 400
    r = webtoon._get(__import__("requests").Session(), remote, get_cookie())
    if r.status_code != 200 or '<div class="viewer_img' not in r.text:
        return jsonify(error="unavailable", message="webtoons.com did not offer this episode "
                       "to the web viewer (404 / Connect Error).")
    imgs = webtoon.VIEWER_IMG_RE.findall(r.text)
    if not imgs:
        return jsonify(error="unavailable", message="Episode exists but no images found.")
    proxied = ["/proxy?" + urllib.parse.urlencode({"url": u, "ref": remote}) for u in imgs]
    return jsonify(images=proxied, remote=True)


@app.get("/proxy")
def proxy_image():
    url = request.args.get("url", "")
    ref = request.args.get("ref", "https://www.webtoons.com/en/")
    host = urllib.parse.urlparse(url).hostname or ""
    if not (host.endswith("pstatic.net") or host.endswith("webtoons.com")):
        abort(400)
    rr = webtoon._headers(get_cookie(), referer=ref)
    import requests as rq
    r = rq.get(url, headers=rr, timeout=60, stream=True)
    return Response(r.iter_content(65536), content_type=r.headers.get(
        "Content-Type", "application/octet-stream"))


@app.get("/media/<title_no>/<folder>/<filename>")
def media(title_no, folder, filename):
    if not (title_no.isdigit() and re.fullmatch(r"ep_\d{4}", folder)
            and re.fullmatch(r"[\w.-]+", filename)):
        abort(404)
    path = os.path.join(db.DOWNLOADS_DIR, title_no, folder, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path)


@app.post("/api/progress")
def api_progress():
    data = request.get_json(force=True, silent=True) or {}
    sid, ep = int(data.get("series_id", 0)), int(data.get("episode_no", 0))
    frac = float(data.get("scroll_frac", 0.0))
    px = int(float(data.get("scroll_px", 0)))
    profile = current_profile()
    if not (sid and ep):
        return jsonify(error="bad data"), 400
    conn = db.connect()
    with conn:
        conn.execute(
            "INSERT INTO progress (profile_id, series_id, episode_no, scroll_frac,"
            " scroll_px, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(profile_id, series_id, episode_no) DO UPDATE SET"
            " scroll_frac=excluded.scroll_frac, scroll_px=excluded.scroll_px,"
            " updated_at=excluded.updated_at",
            (profile["id"], sid, ep, frac, px, db.now()))
    conn.close()
    return jsonify(ok=True)


@app.get("/read/<int:series_id>")
def read_continue(series_id):
    profile = current_profile()
    conn = db.connect()
    prog = conn.execute("SELECT episode_no FROM progress WHERE series_id=? AND profile_id=?"
                        " ORDER BY updated_at DESC LIMIT 1", (series_id, profile["id"])).fetchone()
    series = conn.execute("SELECT max_episode_no FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    if prog:
        return redirect(url_for("reader", series_id=series_id, ep_no=prog["episode_no"]))
    if series and series["max_episode_no"]:
        return redirect(url_for("reader", series_id=series_id, ep_no=1))
    return render_template("error.html", message="No episodes known yet. Open the series page "
                                                  "and refresh or add episodes manually.")


@app.route("/reader/<int:series_id>/<int:ep_no>")
def reader(series_id, ep_no):
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not series:
        abort(404)
    prev_ep = conn.execute(
        "SELECT episode_no FROM episodes WHERE series_id=? AND episode_no<? AND known=1"
        " ORDER BY episode_no DESC LIMIT 1", (series_id, ep_no)).fetchone()
    next_ep = conn.execute(
        "SELECT episode_no FROM episodes WHERE series_id=? AND episode_no>? AND known=1"
        " ORDER BY episode_no LIMIT 1", (series_id, ep_no)).fetchone()
    ep = conn.execute("SELECT * FROM episodes WHERE series_id=? AND episode_no=?",
                      (series_id, ep_no)).fetchone()
    profile = current_profile()
    prog = conn.execute(
        "SELECT * FROM progress WHERE series_id=? AND episode_no=? AND profile_id=?",
        (series_id, ep_no, profile["id"])).fetchone()
    conn.close()
    return render_template(
        "reader.html", s=series, ep=ep, ep_no=ep_no,
        prev_ep=prev_ep["episode_no"] if prev_ep else None,
        next_ep=next_ep["episode_no"] if next_ep else None,
        start_px=(prog["scroll_px"] if prog else 0),
        start_frac=(prog["scroll_frac"] if prog else 0))


@app.route("/settings")
def settings_page():
    av = scanner.clamav_path()
    return render_template("settings.html",
                           cookie=get_cookie(),
                           check_delay=get_setting("check_delay", "1.0"),
                           img_delay=get_setting("img_delay", "0.35"),
                           scan_downloads=_setting_bool("scan_downloads"),
                           scan_antivirus=_setting_bool("scan_antivirus"),
                           av_available=bool(av), av_path=av or "",
                           host_limits=net.stats())


@app.post("/api/settings")
def api_settings_save():
    set_setting("webtoon_cookie", request.form.get("webtoon_cookie", "").strip())
    for key in ("check_delay", "img_delay"):
        try:
            v = max(0.0, float(request.form.get(key, 0)))
        except ValueError:
            v = 0.0
        set_setting(key, str(v))
    for key in ("scan_downloads", "scan_antivirus"):
        set_setting(key, "1" if request.form.get(key) else "0")
    return redirect(url_for("settings_page"))


# ---------------------------------------------------------------- Phoenix Records file

def write_phoenix_records():
    conn = db.connect()
    rows = conn.execute("SELECT * FROM series ORDER BY title COLLATE NOCASE").fetchall()
    lines = [
        "# Phoenix Records",
        "",
        f"_Generated {db.now()} by NetScrollOffline._",
        "",
        "Permanently archived series and their current state. Devices only borrow copies;",
        "this PC library is the canon. Locked = not readable via the web viewer for the",
        "account in use at check time.",
        "",
    ]
    for s in rows:
        known = conn.execute("SELECT COUNT(*) AS c FROM episodes WHERE series_id=? AND known=1",
                             (s["id"],)).fetchone()["c"]
        dl = conn.execute("SELECT COUNT(*) AS c FROM episodes WHERE series_id=? AND status='downloaded'",
                          (s["id"],)).fetchone()["c"]
        locked = conn.execute("SELECT COUNT(*) AS c FROM episodes WHERE series_id=? AND status='locked'",
                              (s["id"],)).fetchone()["c"]
        kind = "Manga (page-by-page)" if s["kind"] == "manga" else "Manhwa/Webtoon (scroll)"
        genre_bits = " ".join(w.capitalize() for w in (s["genre"] or "").split("-") if w) \
                     + (f", {s['genre_tags']}" if s["genre_tags"] else "")
        total = max(s["max_episode_no"], dl, known)
        lines += [
            f"## {s['title']}",
            f"- **Type / tags:** {kind}{(' · ' + genre_bits) if genre_bits.strip(' ,') else ''}",
            f"- **Series status:** {s['series_status'] or 'Unknown (check on site)'}",
            f"- **Episodes:** {dl} in Phoenix Records" +
            (f" out of ~{total} known to exist" if total else "") +
            (f" · {known} known/local-listed" if known != dl else "") +
            (f" · {locked} still locked" if locked else ""),
            (f"- **Description:** {s['description'][:220]}" if s["description"] else ""),
            "",
        ]
    conn.close()
    os.makedirs(db.DOWNLOADS_DIR, exist_ok=True)
    path = os.path.join(db.DOWNLOADS_DIR, "PHOENIX_RECORDS.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ---------------------------------------------------------------- watchlist

def _norm(name: str) -> str:
    s = (name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[\[\]'\u2019'’]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def do_watchlist_scan(job_id, limit=999):
    """Match unmatched watchlist names against webtoons.com's live search."""
    cookie = get_cookie()
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM watchlist WHERE match_status='unmatched' ORDER BY id LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    _update_job(job_id, total=len(rows))
    for i, row in enumerate(rows):
        _check_cancel(job_id)
        names = [row["name"]]
        extra = re.split(r"\s*[/;]\s*", row["notes"] or "")
        extra = [e for e in extra if e and not e.lower().startswith("removed")] or ""
        for alias in (extra if extra else []):
            names.append(alias)
        candidates, best, best_ratio = [], None, 0.0
        seen_tn = set()
        for nm in names:
            for c in webtoon.search_immediate(nm, cookie):
                if c["title_no"] in seen_tn:
                    continue
                seen_tn.add(c["title_no"])
                candidates.append(c)
            time.sleep(0.6)
        for c in candidates:
            r = difflib.SequenceMatcher(None, _norm(row["name"]), _norm(c["title"])).ratio()
            if r > best_ratio:
                best_ratio, best = r, c
        if best and best_ratio >= 0.55:
            try:
                parts = webtoon.resolve_title_noURL(best["title_no"])
                meta = webtoon.estimate_series(parts, cookie)
                conn = db.connect()
                with conn:
                    conn.execute(
                        "UPDATE watchlist SET match_status='matched', title_no=?, title=?,"
                        " language=?, genre=?, slug=?, ep_total_est=?, last_checked=? WHERE id=?",
                        (parts["title_no"], meta["title"], parts["language"], parts["genre"],
                         parts["slug"], meta.get("ep_total") or len(meta["episodes"]),
                         db.now(), row["id"]))
                conn.close()
                msg = f"{row['name']} → {meta['title']} (Ep ~{meta.get('ep_total')}"
                if meta["status"]:
                    msg += f", {meta['status']}"
                msg += ")"
            except Exception as e:
                conn = db.connect()
                with conn:
                    conn.execute(
                        "UPDATE watchlist SET match_status='unmatched', last_checked=? WHERE id=?",
                        (db.now(), row["id"]))
                conn.close()
                msg = f"{row['name']}: match error {str(e)[:80]}"
        else:
            conn = db.connect()
            with conn:
                conn.execute("UPDATE watchlist SET match_status='not_found', last_checked=? WHERE id=?",
                             (db.now(), row["id"]))
            conn.close()
            msg = f"{row['name']}: no confident match — needs a source hunt"
        _update_job(job_id, done=i + 1, message=msg)
        _sleep_or_cancel(job_id, 0.4)
    write_phoenix_records()


# ---------------------------------------------------------------- records & watchlist routes

@app.get("/api/records/generate")
def api_records_generate():
    path = write_phoenix_records()
    return jsonify(ok=True, path=path, url="/records-file")


@app.get("/records-file")
def records_file():
    write_phoenix_records()
    return send_file(os.path.join(db.DOWNLOADS_DIR, "PHOENIX_RECORDS.md"), as_attachment=False)


@app.route("/watchlist")
def watchlist_page():
    conn = db.connect()
    rows = conn.execute("""
        SELECT w.*, s.id AS series_id
        FROM watchlist w LEFT JOIN series s ON s.title_no = w.title_no
        ORDER BY CASE WHEN w.match_status='matched' THEN 0 ELSE 1 END,
                 w.ep_total_est DESC, w.name COLLATE NOCASE""").fetchall()
    conn.close()
    return render_template("watchlist.html", rows=rows)


@app.post("/api/watchlist/import")
def api_watchlist_import():
    seed_path = os.path.join(os.path.dirname(db.DB_PATH), "watchlist.json")
    if not os.path.exists(seed_path):
        return jsonify(error=f"No seed file at {seed_path}"), 404
    items = json.load(open(seed_path, encoding="utf-8"))
    n = 0
    conn = db.connect()
    with conn:
        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            aliases = item.get("aliases") or []
            notes = " / ".join(([item.get("notes")] if item.get("notes") else []) + aliases) or ""
            n += conn.execute(
                "INSERT OR IGNORE INTO watchlist (name, notes) VALUES (?, ?)", (name, notes)).rowcount
    conn.close()
    return jsonify(ok=True, added=n)


@app.post("/api/watchlist/scan")
def api_watchlist_scan():
    limit = int(request.form.get("limit", 999) or 999)
    job_id = _new_job("watchlist-scan", kind="watchlist",
                      fn=lambda: do_watchlist_scan(job_id, limit))
    return jsonify(ok=True, job=job_id)


@app.post("/api/watchlist/flag")
def api_watchlist_flag():
    wid = int(request.form.get("id") or 0)
    unlock = (request.form.get("unlock") or "unknown").strip()
    if unlock not in ("unknown", "free", "daily_pass", "coins", "other"):
        unlock = "unknown"
    conn = db.connect()
    with conn:
        conn.execute("UPDATE watchlist SET unlock_type=? WHERE id=?", (unlock, wid))
    conn.close()
    write_phoenix_records()
    return jsonify(ok=True)


@app.post("/api/watchlist/remove")
def api_watchlist_remove():
    wid = int(request.form.get("id") or 0)
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    conn.close()
    return jsonify(ok=True)


@app.post("/api/watchlist/add")
def api_watchlist_add():
    """Add a matched watchlist entry to the library and drop it from the list."""
    wid = int(request.form.get("id") or 0)
    conn = db.connect()
    row = conn.execute("SELECT * FROM watchlist WHERE id=?", (wid,)).fetchone()
    in_lib = conn.execute("SELECT id FROM series WHERE title_no=?",
                          (row["title_no"],)).fetchone() if row else None
    conn.close()
    if not row or row["match_status"] != "matched" or not row["title_no"]:
        return jsonify(error="Entry is not matched to a series yet."), 400
    if in_lib:
        conn = db.connect()
        with conn:
            conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
        conn.close()
        return jsonify(ok=True, series_url=url_for("series_page", series_id=in_lib["id"]))
    base = f"https://www.webtoons.com/{row['language']}/{row['genre']}/{row['slug']}"
    parts = webtoon.parse_series_url(f"{base}/list?title_no={row['title_no']}")
    meta = webtoon.get_series_meta(parts, cookie=get_cookie())
    conn = db.connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO series (title_no, language, genre, slug, kind, genre_tags, title,"
            " authors, description, cover, cover_kind, series_status, max_episode_no, last_checked)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (parts["title_no"], parts["language"], parts["genre"], parts["slug"], "webtoon",
             "", meta["title"], meta["authors"], meta["description"], meta["cover"],
             meta["cover_kind"], meta["status"], 0, db.now()))
        series_id = cur.lastrowid
        for ep in meta["episodes"]:
            conn.execute(
                "INSERT INTO episodes (series_id, episode_no, title, url, known, status)"
                " VALUES (?,?,?,?,1,'unknown')",
                (series_id, ep["episode_no"], ep["title"], ep["url"]))
            conn.execute("UPDATE series SET max_episode_no=max(max_episode_no,?) WHERE id=?",
                         (ep["episode_no"], series_id))
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    conn.close()
    write_phoenix_records()
    return jsonify(ok=True, series_url=url_for("series_page", series_id=series_id))


@app.route("/notes")  # legacy alias
def notes_redirect():
    return redirect(url_for("records_file"))


if __name__ == "__main__":
    db.init_db()
    import socket

    lan_ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    lan_ips.discard("0.0.0.0")
    lan_ips.add("127.0.0.1")

    print("\n  NetScrollOffline is running — open any of these:", flush=True)
    for ip in sorted(lan_ips):
        print(f"    http://{ip}:8767", flush=True)
    print("  Devices on the same Wi-Fi can use the non-127.0.0.1 address;", flush=True)
    print("  for offline installs & use-away-from-home, see README → 'On your phone'.", flush=True)

    app.run(host="0.0.0.0", port=8767, debug=False)
