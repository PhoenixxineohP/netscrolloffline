"""NetScrollOffline - a small local web app for owning your webtoon library."""
import os, re, time, queue, threading, json, urllib.parse, difflib
import zipfile, tempfile, html, shutil
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, Response, send_file, send_from_directory, abort)
import db
import webtoon
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
JOBS = {}
JOB_QUEUE = queue.Queue()
JOB_LOCK = threading.Lock()


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


def _job_status(job_id):
    with JOB_LOCK:
        return dict(JOBS[job_id]) if job_id in JOBS else None


def _update_job(job_id, **fields):
    with JOB_LOCK:
        JOBS[job_id].update(fields)


def _new_job(desc):
    with JOB_LOCK:
        job_id = f"job{(max((int(k[3:]) for k in JOBS), default=0) + 1)}"
        JOBS[job_id] = {"id": job_id, "status": "queued", "desc": desc, "done": 0,
                        "total": 0, "messages": []}
    return job_id


def worker():
    while True:
        job_id, fn = JOB_QUEUE.get()
        _update_job(job_id, status="running")
        try:
            fn()
        except Exception as e:  # never let the worker die
            _update_job(job_id, status="error", message=str(e))
            import traceback
            traceback.print_exc()
        else:
            st = _job_status(job_id)
            if st["status"] == "running":
                _update_job(job_id, status="done")


threading.Thread(target=worker, daemon=True).start()


def _delay_ge_zero(name):
    try:
        v = float(get_setting(name, "0"))
    except ValueError:
        v = 0
    return max(v, 0.0)


def refresh_series_episodes(series_id, progress_cb=None):
    """Recrawl the web episode list and upsert rows. Returns count found."""
    conn = db.connect()
    series = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    conn.close()
    if not series:
        raise RuntimeError("series not found")
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


def do_refresh(job_id, series_id):
    """Background crawl of the full web episode list."""
    _update_job(job_id, total=0, message="Fetching episode list…")

    def progress(page, found):
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
    for i, ep in enumerate(rows):
        status = webtoon.check_episode(ep["url"], cookie)
        mapped = {"available": "available", "unavailable": "unknown"}.get(status, "unknown")
        conn = db.connect()
        with conn:
            conn.execute("UPDATE episodes SET status=?, error='' WHERE id=?",
                         (mapped, ep["id"]))
        conn.close()
        _update_job(job_id, done=i + 1, message=f"Ep {ep['episode_no']}: {status}")
        if i + 1 < len(rows):
            time.sleep(delay)


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
    for i, ep in enumerate(rows):
        try:
            saved, status = webtoon.download_episode(series, ep, cookie)
        except RequestException as e:
            status, saved = "error", []
            conn = db.connect()
            with conn:
                conn.execute("UPDATE episodes SET status='error', error=? WHERE id=?",
                             (str(e)[:300], ep["id"]))
            conn.close()
            msgs.append(f"Ep {ep['episode_no']}: ERROR {str(e)[:120]}")
            _update_job(job_id, done=i + 1, message=msgs[-1])
            continue
        conn = db.connect()
        with conn:
            if status == "locked":
                conn.execute("UPDATE episodes SET status='locked', error=? WHERE id=?",
                             ("not readable on the web viewer for this account", ep["id"]))
            elif status == "unavailable":
                conn.execute("UPDATE episodes SET status='unknown', error=? WHERE id=?",
                             ("web viewer answered an error (often app-only titles)", ep["id"]))
            else:
                conn.execute(
                    "UPDATE episodes SET status=?, image_count=?, downloaded_at=?, error=''"
                    " WHERE id=?", (status, len(saved), db.now(), ep["id"]))
        conn.close()
        msgs.append(f"Ep {ep['episode_no']}: {status} ({len(saved)} pages)")
        _update_job(job_id, done=i + 1, message=msgs[-1])
        time.sleep(_delay_ge_zero("check_delay"))
    write_phoenix_records()


# ---------------------------------------------------------------- export
EXPORTS_DIR = os.path.join(db.BASE_DIR, "exports")
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
        _update_job(job_id, status="error",
                    message="Nothing downloaded yet — run a download first.")
        return

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
    return render_template("library.html", series=series_rows)


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
        parts = webtoon.parse_series_url(url)
    except ValueError as e:
        return render_template("error.html", message=str(e)), 400
    conn = db.connect()
    dup = conn.execute("SELECT id FROM series WHERE title_no=?", (parts["title_no"],)).fetchone()
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
            " cover, cover_kind, series_status, max_episode_no, last_checked)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (parts["title_no"], parts["lang"], parts["genre"], parts["slug"],
             meta["title"] if meta else "Untitled (needs refresh)",
             meta["authors"] if meta else "", meta["description"] if meta else "",
             meta["cover"] if meta else "", meta["cover_kind"] if meta else "thumb",
             meta["status"] if meta else "", 0, db.now()))
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
    job_id = _new_job("Refreshing episode list")
    JOB_QUEUE.put((job_id, lambda: do_refresh(job_id, series_id)))
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
    job_id = _new_job(f"check ser{series_id} {start}-{end}")
    JOB_QUEUE.put((job_id, lambda: do_check_range(job_id, series_id, start, end)))
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
    conn = db.connect()
    row = conn.execute("SELECT max_episode_no FROM series WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not row:
        return jsonify(error="Unknown series."), 404
    if not end:
        end = row["max_episode_no"]
    job_id = _new_job(f"download ser{sid} {start}-{end}")
    JOB_QUEUE.put((job_id, lambda: do_download(job_id, sid, start, end, full=full)))
    return jsonify(ok=True, job=job_id)


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
    desc = f"export {'ser' + str(sid) if sid else 'library'} {fmt}"
    job_id = _new_job(desc)
    JOB_QUEUE.put((job_id, lambda: do_export(job_id, sid, fmt)))
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
    return render_template("settings.html",
                           cookie=get_cookie(),
                           check_delay=get_setting("check_delay", "1.0"),
                           img_delay=get_setting("img_delay", "0.35"))


@app.post("/api/settings")
def api_settings_save():
    set_setting("webtoon_cookie", request.form.get("webtoon_cookie", "").strip())
    for key in ("check_delay", "img_delay"):
        try:
            v = max(0.0, float(request.form.get(key, 0)))
        except ValueError:
            v = 0.0
        set_setting(key, str(v))
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
        time.sleep(0.4)
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
    job_id = _new_job("watchlist-scan")
    JOB_QUEUE.put((job_id, lambda: do_watchlist_scan(job_id, limit)))
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
