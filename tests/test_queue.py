"""Offline tests for the NetScrollOffline job queue (no network).

Run from the project root:  python tests/test_queue.py
"""
import os, sys, time, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import tempfile

# Keep the test suite away from the real library: use a throwaway database and
# download/export folders under the system temp dir.
_TMP = tempfile.mkdtemp(prefix="nso-queue-test-")
os.environ["NSO_DATA_DIR"] = _TMP

import db
db.init_db()

import app
import webtoon

app.RETRY_DELAY = 0.2

FAILS = {}
NOTES = []
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail!r}")


def fake_download(series, ep, cookie=None, progress_cb=None):
    NOTES.append(("download", ep["episode_no"]))
    if FAILS.get(ep["episode_no"], 0) > 0:
        FAILS[ep["episode_no"]] -= 1
        raise webtoon.requests.exceptions.ConnectionError("boom")
    return ["/tmp/x1.jpg"], "downloaded"


webtoon.download_episode = fake_download
app.refresh_series_episodes = lambda sid, progress_cb=None: (NOTES.append(("refresh", sid)) or 3)
webtoon.check_episode = lambda url, cookie=None: (NOTES.append(("check", url)) or "available")


def mk_series(title_no="tn1", title="Test Series", n=3):
    conn = db.connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO series (title_no, language, genre, slug, title,"
            " max_episode_no) VALUES (?,?,?,?,?,?)",
            (title_no, "en", "action", "slug-" + title_no, title, n))
        sid = conn.execute("SELECT id FROM series WHERE title_no=?",
                           (title_no,)).fetchone()["id"]
        for i in range(1, n + 1):
            conn.execute(
                "INSERT OR REPLACE INTO episodes (series_id, episode_no, title, url,"
                " known, status, error) VALUES (?,?,?,?,1,'unknown','')",
                (sid, i, f"Ep {i}", f"https://example/{i}"))
    conn.close()
    return sid


def wait(job_id, timeout=20, want=("done", "error", "cancelled")):
    end = time.time() + timeout
    while time.time() < end:
        st = app._job_status(job_id)
        if st and st["status"] in want:
            return st
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not settle: {app._job_status(job_id)}")


def reset_queue(slots=1, auto_retry=True, max_attempts=3):
    with app.JOBS_LOCK:
        app.JOBS.clear()
        app.QUEUE.update(slots=slots, paused=False, auto_retry=auto_retry,
                         max_attempts=max_attempts)
        app.DISPATCH.notify_all()
    app._QUEUE_SETTINGS_LOADED = True  # keep settings out of the way


app._QUEUE_SETTINGS_LOADED = True
client = app.app.test_client()


def post(url, data=None):
    return client.post(url, data=data or {})


def spawn(desc, body, kind="task", series_id=None, **kw):
    """Create a job whose callable is body(job_id)."""
    holder = {}

    def fn():
        body(holder["id"])

    jid = app._new_job(desc, fn=fn, kind=kind, series_id=series_id, **kw)
    holder["id"] = jid
    return jid


# ---------------------------------------------------------------- 1. slot limit
reset_queue(1)
gate = threading.Event()
a = spawn("A", lambda j: gate.wait(5))
b = spawn("B", lambda j: gate.wait(5))
time.sleep(0.4)
check("slot limit: second job queued", app._job_status(b)["status"] == "queued",
      app._job_status(b)["status"])
check("slot limit: first job running", app._job_status(a)["status"] == "running")
check("slot limit: exactly one running", app._queue_snapshot()["running"] == 1)
gate.set()
wait(a); wait(b)
check("slot limit: both done", app._job_status(a)["status"] == "done" and
      app._job_status(b)["status"] == "done")

# ---------------------------------------------------------------- 2. two slots
reset_queue(2)
gate2 = threading.Event()
started = []


def slow2(tag):
    started.append(tag)
    gate2.wait(5)


a = spawn("A2", lambda j: slow2("A"))
b = spawn("B2", lambda j: slow2("B"))
c = spawn("C2", lambda j: slow2("C"))
time.sleep(0.5)
check("slots=2: two start, third waits", len(started) == 2 and
      app._queue_snapshot()["running"] == 2, (started, app._queue_snapshot()["running"]))
gate2.set()
for j in (a, b, c):
    wait(j)

# ---------------------------------------------------------------- 3. pause / resume
reset_queue(1)
post("/api/queue/pause")
a = spawn("P", lambda j: time.sleep(0.05))
time.sleep(0.3)
check("pause: job stays queued", app._job_status(a)["status"] == "queued")
post("/api/queue/resume")
wait(a)
check("pause: resumes", app._job_status(a)["status"] == "done")

# ---------------------------------------------------------------- 4. cancel queued
reset_queue(1)
gate3 = threading.Event()
a = spawn("A3", lambda j: gate3.wait(5))
b = spawn("B3", lambda j: NOTES.append(("should-not-run",)))
time.sleep(0.3)
r = post(f"/api/queue/cancel/{b}")
check("cancel queued: status cancelled", app._job_status(b)["status"] == "cancelled",
      (r.status_code, app._job_status(b)["status"]))
gate3.set()
wait(a)
time.sleep(0.3)
check("cancel queued: never ran", ("should-not-run",) not in NOTES)

# ---------------------------------------------------------------- 5. cancel running
reset_queue(1)
a = spawn("CR", lambda j: [app._check_cancel(j) or time.sleep(0.01) for _ in range(300)])
time.sleep(0.25)
post(f"/api/queue/cancel/{a}")
st = wait(a, 8)
check("cancel running: cancelled", st["status"] == "cancelled", st)

# ---------------------------------------------------------------- 6. auto retry
reset_queue(1)
calls = {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] < 3:
        raise RuntimeError("nope")


a = app._new_job("R", kind="download", fn=flaky)
st = wait(a, 20)
check("auto retry: succeeded after retries", st["status"] == "done" and calls["n"] == 3,
      (st["status"], calls["n"], st["attempts"]))

# ---------------------------------------------------------------- 7. retry limit
reset_queue(1)
calls2 = {"n": 0}


def always_fail():
    calls2["n"] += 1
    raise RuntimeError("always")


a = app._new_job("RF", kind="download", fn=always_fail, max_attempts=2)
st = wait(a, 20)
time.sleep(0.8)
st = app._job_status(a)
check("retry limit: stops at max attempts", st["status"] == "error" and calls2["n"] == 2,
      (st["status"], calls2["n"], st["attempts"]))

# ---------------------------------------------------------------- 8. auto retry disabled
reset_queue(1, auto_retry=False)
calls3 = {"n": 0}


def failer():
    calls3["n"] += 1
    raise RuntimeError("no retry")


a = app._new_job("NR", kind="download", fn=failer)
st = wait(a)
time.sleep(0.8)
check("auto retry off: single attempt", st["status"] == "error" and calls3["n"] == 1,
      (st["status"], calls3["n"]))

# ---------------------------------------------------------------- 9. download partial retry
reset_queue(1)
sid = mk_series("tn2", "Partial", 3)
FAILS[2] = 1
NOTES.clear()
jid = spawn("D", lambda j: app.do_download(j, sid, 1, 3, full=False),
            kind="download", series_id=sid)
st = wait(jid, 25)
time.sleep(0.4)
st = app._job_status(jid)
dls = [n[1] for n in NOTES if n[0] == "download"]
check("download: eventually done", st["status"] == "done", st)
check("download: retry only reprocessed failed episode",
      dls.count(2) == 2 and dls.count(1) == 1 and dls.count(3) == 1, dls)
conn = db.connect()
bad = conn.execute("SELECT COUNT(*) c FROM episodes WHERE series_id=? AND status!='downloaded'",
                   (sid,)).fetchone()["c"]
conn.close()
check("download: episodes all stored", bad == 0, bad)

# ---------------------------------------------------------------- 10. unavailable counted
reset_queue(1, auto_retry=False)
sid2 = mk_series("tn4", "Unavailable", 2)
webtoon.download_episode = lambda s, e, c=None, progress_cb=None: ([], "unavailable")
jid = spawn("U", lambda j: app.do_download(j, sid2, 1, 2, full=False),
            kind="download", series_id=sid2)
st = wait(jid)
check("download: unavailable marks job error",
      st["status"] == "error" and st["failed"] == 2, (st["status"], st["failed"]))
webtoon.download_episode = fake_download

# ---------------------------------------------------------------- 11. manual retry
reset_queue(1, auto_retry=False)
calls4 = {"n": 0}


def fail_once():
    calls4["n"] += 1
    if calls4["n"] == 1:
        raise RuntimeError("x")


a = app._new_job("M", kind="download", fn=fail_once)
st = wait(a)
check("manual retry: first run errors", st["status"] == "error")
r = post(f"/api/queue/retry/{a}")
st = wait(a)
check("manual retry: succeeds", st["status"] == "done" and r.status_code == 200, st)

# ---------------------------------------------------------------- 12. clear finished
reset_queue(1)
a = app._new_job("C2", fn=lambda: None)
wait(a)
r = post("/api/queue/clear")
check("clear: finished removed", not app.JOBS and r.status_code == 200, list(app.JOBS))

# ---------------------------------------------------------------- 13. endpoints
reset_queue(1)
gate4 = threading.Event()
q1 = spawn("Q1", lambda j: gate4.wait(5))
q2 = spawn("Q2", lambda j: None)
time.sleep(0.3)
r = post("/api/queue/cancel-queued")
check("cancel-queued endpoint", app._job_status(q2)["status"] == "cancelled",
      (r.status_code, app._job_status(q2)["status"]))
gate4.set()
wait(q1)
r = post(f"/api/queue/remove/{q2}")
check("remove job", app._job_status(q2) is None and r.status_code == 200)

reset_queue(1)
gate5 = threading.Event()
q3 = spawn("Q3", lambda j: gate5.wait(5))
time.sleep(0.25)
check("remove running rejected", post(f"/api/queue/remove/{q3}").status_code == 409)
check("retry running rejected", post(f"/api/queue/retry/{q3}").status_code == 409)
gate5.set()
wait(q3)
check("unknown job 404", post("/api/queue/remove/jobNOPE").status_code == 404)

# ---------------------------------------------------------------- 14. retry-failed
reset_queue(1, auto_retry=False)
n = {"n": 0}


def fail_first():
    n["n"] += 1
    if n["n"] == 1:
        raise RuntimeError("k")


a = app._new_job("F", kind="download", fn=fail_first)
wait(a)
post("/api/queue/retry-failed")
st = wait(a)
check("retry-failed endpoint", st["status"] == "done", st)

# ---------------------------------------------------------------- 15. settings
reset_queue(2)
r = post("/api/queue/slots", {"slots": "3"})
check("slots endpoint accepts 3", r.get_json().get("slots") == 3, r.get_json())
check("slots endpoint rejects 0", post("/api/queue/slots", {"slots": "0"}).status_code == 400)
check("slots endpoint rejects junk", post("/api/queue/slots", {"slots": "x"}).status_code == 400)
check("slots endpoint clamps to 8",
      post("/api/queue/slots", {"slots": "99"}).get_json().get("slots") == 8)
check("settings: slots saved", app.get_setting("queue_slots") == "8")
js = post("/api/queue/auto-retry", {"enabled": "0", "max_attempts": "5"}).get_json()
check("auto-retry toggle", js["auto_retry"] is False and js["max_attempts"] == 5, js)
check("settings: auto retry saved",
      app.get_setting("queue_auto_retry") == "0" and
      app.get_setting("queue_max_attempts") == "5")
post("/api/queue/auto-retry", {"enabled": "1", "max_attempts": "3"})

# ---------------------------------------------------------------- 16. snapshot + pages
reset_queue(2)
js = client.get("/api/queue").get_json()
check("snapshot shape",
      {"jobs", "slots", "running", "paused", "queued", "auto_retry"} <= set(js), js)
check("snapshot hides fn/cancel", all("fn" not in j and "cancel" not in j
                                      for j in js["jobs"]))
r = client.get("/queue")
check("queue page renders", r.status_code == 200 and b"queue.js" in r.data)
check("api_job unknown empty", client.get("/api/job/jobNOPE").get_json() == {})

# ---------------------------------------------------------------- 17. endpoint jobs
reset_queue(1)
sid3 = mk_series("tn5", "Route Test", 3)
js = post(f"/api/series/{sid3}/refresh").get_json()
check("refresh endpoint returns job", bool(js.get("job")), js)
if js.get("job"):
    check("refresh job done", wait(js["job"])["status"] == "done")
reset_queue(1)
js = post(f"/api/series/{sid3}/check", {"start": "1", "end": "3"}).get_json()
check("check endpoint returns job", bool(js.get("job")), js)
if js.get("job"):
    check("check job done", wait(js["job"])["status"] == "done")

# ---------------------------------------------------------------- 18. download endpoint queues
reset_queue(1)
js = post("/api/download", {"series_id": sid3, "start": "1", "end": "3"}).get_json()
check("download endpoint returns job", bool(js.get("job")), js)
if js.get("job"):
    st = wait(js["job"], 25)
    check("download endpoint job done", st["status"] == "done", st)

# ---------------------------------------------------------------- 19. bulk queue
reset_queue(2)
sid4 = mk_series("tn6", "Bulk A", 1)
sid5 = mk_series("tn7", "Bulk B", 1)
js = post("/api/queue/bulk", {"series_ids": f"{sid4},{sid5}"}).get_json()
check("bulk: queues every series", js.get("queued") == 2, js)
check("bulk: no failures", js.get("failed") == 0, js)
check("bulk: creates download jobs",
      sum(1 for j in app._queue_snapshot()["jobs"]
          if j["kind"] == "download" and j["series_id"] in (sid4, sid5)) == 2)
for j in app._queue_snapshot()["jobs"]:
    if j["kind"] == "download" and j["series_id"] in (sid4, sid5):
        check(f"bulk: job for {j['series_id']} done", wait(j["id"], 25)["status"] == "done")

reset_queue(1)
sid6 = mk_series("tn8", "Bulk C", 1)
post("/api/queue/bulk", {"series_ids": str(sid6)})
js = post("/api/queue/bulk", {"series_ids": str(sid6)}).get_json()
check("bulk: duplicate series skipped", js.get("skipped") == 1 and js.get("queued") == 0, js)
check("bulk: rejects empty selection",
      post("/api/queue/bulk", {"series_ids": ""}).status_code == 400)
check("bulk: rejects junk ids",
      post("/api/queue/bulk", {"series_ids": "abc"}).status_code == 400)
js = post("/api/queue/bulk", {"series_ids": "999999"}).get_json()
check("bulk: unknown series counted as failed", js.get("failed") == 1, js)
# single endpoint also dedupes an already-queued full download
js = post("/api/download", {"series_id": sid6, "start": 1, "end": 0, "full": 1}).get_json()
check("download endpoint reports already queued", bool(js.get("skipped")), js)
r = client.get("/")
check("library page offers bulk queue",
      r.status_code == 200 and b"qpick-all" in r.data and b"queuebulk.js" in r.data)

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
