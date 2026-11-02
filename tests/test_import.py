"""Offline tests for adopting existing downloads into the library.

Run from the project root:  python tests/test_import.py
No network access: the metadata lookup is forced off.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_TMP = tempfile.mkdtemp(prefix="nso-import-test-")
os.environ["NSO_DATA_DIR"] = _TMP

import db
db.init_db()

import localimport

failures = []


def check(name, cond, detail=None):
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail!r}")


JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000


def make_episode(path, pages=2):
    os.makedirs(path, exist_ok=True)
    for i in range(pages):
        with open(os.path.join(path, f"{i:03d}.jpg"), "wb") as f:
            f.write(JPG)


ROOT_DL = os.path.join(_TMP, "downloads")
make_episode(os.path.join(ROOT_DL, "95", "ep_0001"), 2)
make_episode(os.path.join(ROOT_DL, "95", "ep_0002"), 3)
make_episode(os.path.join(ROOT_DL, "77", "ep_1"), 1)          # old unpadded name
make_episode(os.path.join(ROOT_DL, "md_1234abcd", "ep_0001"), 2)
make_episode(os.path.join(ROOT_DL, "tapas_42", "12"), 1)      # bare numeric folder
os.makedirs(os.path.join(ROOT_DL, "no_episodes", "notes"), exist_ok=True)
os.makedirs(os.path.join(ROOT_DL, "empty_ep", "ep_0001"), exist_ok=True)
os.makedirs(os.path.join(ROOT_DL, "95", "ep_0003"), exist_ok=True)
with open(os.path.join(ROOT_DL, "95", "ep_0003", "000.jpg"), "wb") as f:
    f.write(JPG)
with open(os.path.join(ROOT_DL, "95", "ep_0003", "000.jpg.part"), "w") as f:
    f.write("partial")

# ---------------------------------------------------------------- scan
found = localimport.scan(ROOT_DL)
keys = {s["key"]: s for s in found}
check("scan: finds series folders", set(keys) == {"95", "77", "md_1234abcd", "tapas_42"},
      set(keys))
check("scan: ignores folders without episode images", "no_episodes" not in keys
      and "empty_ep" not in keys)
check("scan: reads padded ep dirs", [e["no"] for e in keys["95"]["episodes"]] == [1, 2, 3])
check("scan: reads old unpadded ep dirs", [e["no"] for e in keys["77"]["episodes"]] == [1])
check("scan: reads bare numeric ep dirs", [e["no"] for e in keys["tapas_42"]["episodes"]] == [12])
check("scan: flags partial downloads",
      [e["partial"] for e in keys["95"]["episodes"] if e["no"] == 3] == [True])
check("scan: counts images",
      [e["images"] for e in keys["95"]["episodes"] if e["no"] == 2][0].__len__() == 3)

# Pointing at one series folder must work too
single = localimport.scan(os.path.join(ROOT_DL, "95"))
check("scan: accepts a single series folder",
      len(single) == 1 and single[0]["key"] == "95", single)

# Pointing at an app folder must descend into downloads/
app_folder = tempfile.mkdtemp(prefix="nso-appfolder-")
os.makedirs(os.path.join(app_folder, "data"))
os.rename(ROOT_DL, os.path.join(app_folder, "downloads"))
check("scan: descends into downloads/",
      len(localimport.scan(app_folder)) == 4)
os.rename(os.path.join(app_folder, "downloads"), ROOT_DL)

# Bad input
bad = False
try:
    localimport.scan(os.path.join(_TMP, "does-not-exist"))
except localimport.ImportError_:
    bad = True
check("scan: missing folder raises", bad)

# The import panel must be visible even when the library is EMPTY — that is
# exactly the situation it exists to fix. It regressed once by living inside
# the "{% if series %}" block.
import app

_empty_client = app.app.test_client()
check("app: import panel visible on an empty library",
      b"importbox" in _empty_client.get("/").data)
check("app: import panel offers the downloads path",
      db.DOWNLOADS_DIR.encode() in _empty_client.get("/").data)

# ---------------------------------------------------------------- adopt
stats = localimport.adopt(found, lookup=False)
check("adopt: reports series count", stats["series"] == 4, stats)
check("adopt: reports new series", stats["new_series"] == 4, stats)
check("adopt: reports episodes", stats["episodes"] == 6, stats)
check("adopt: reports partial", stats["partial"] == 1, stats)

conn = db.connect()
rows = {r["title_no"]: r for r in conn.execute("SELECT * FROM series")}
check("adopt: creates webtoon series", rows["95"]["source"] == "webtoon")
check("adopt: infers mangadex source", rows["md_1234abcd"]["source"] == "mangadex")
check("adopt: infers tapas source", rows["tapas_42"]["source"] == "tapas")
check("adopt: placeholder titles are readable",
      rows["95"]["title"] == "WEBTOON #95", rows["95"]["title"])
check("adopt: max_episode_no set", rows["95"]["max_episode_no"] == 3,
      rows["95"]["max_episode_no"])

eps = {r["episode_no"]: r for r in conn.execute(
    "SELECT * FROM episodes WHERE series_id=?", (rows["95"]["id"],))}
check("adopt: marks episodes downloaded",
      eps[1]["status"] == "downloaded" and eps[2]["status"] == "downloaded")
check("adopt: records image counts", eps[2]["image_count"] == 3, eps[2]["image_count"])
check("adopt: partial episode is not marked downloaded",
      eps[3]["status"] == "unknown" and "incomplete" in eps[3]["error"],
      (eps[3]["status"], eps[3]["error"]))
check("adopt: sets downloaded_at", bool(eps[1]["downloaded_at"]))
check("adopt: does not claim site knowledge", eps[1]["known"] == 0)
conn.close()

# Re-running must be idempotent
stats2 = localimport.adopt(found, lookup=False)
check("adopt: second run adds no series", stats2["new_series"] == 0, stats2)
check("adopt: second run adds no episodes", stats2["new_episodes"] == 0, stats2)
conn = db.connect()
check("adopt: no duplicate series",
      conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 4)
check("adopt: no duplicate episodes",
      conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 6)
conn.close()

# A later offline run must not replace a real title with a placeholder
conn = db.connect()
with conn:
    conn.execute("UPDATE series SET title='Tower of God' WHERE title_no='95'")
conn.close()
localimport.adopt(found, lookup=False)
conn = db.connect()
check("adopt: offline run keeps existing title",
      conn.execute("SELECT title FROM series WHERE title_no='95'").fetchone()[0]
      == "Tower of God")
conn.close()

# ---------------------------------------------------------------- app wiring
client = app.app.test_client()
check("app: library shows import panel", b"importbox" in client.get("/").data)
check("app: library shows downloads path", db.DOWNLOADS_DIR.encode() in client.get("/").data)
check("app: import rejects a missing folder",
      client.post("/api/library/import", data={"path": "/nope"}).status_code == 400)
empty_dir = tempfile.mkdtemp(prefix="nso-empty-")
check("app: import reports an empty folder",
      client.post("/api/library/import", data={"path": empty_dir}).status_code == 404)

# Import through the real route/job, into a clean database
_TMP2 = tempfile.mkdtemp(prefix="nso-import-app-")
db.DB_PATH = os.path.join(_TMP2, "data", "netscroll.db")
db.DOWNLOADS_DIR = os.path.join(_TMP2, "downloads")
os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
db.init_db()
make_episode(os.path.join(db.DOWNLOADS_DIR, "555", "ep_0001"), 2)
res = client.post("/api/library/import",
                  data={"path": db.DOWNLOADS_DIR, "lookup": "0"}).get_json()
check("app: import starts a job", bool(res.get("job")), res)
for _ in range(200):
    st = app._job_status(res["job"])
    if st["status"] in ("done", "error", "cancelled"):
        break
    time.sleep(0.1)
st = app._job_status(res["job"])
check("app: import job finishes", st["status"] == "done", st)
check("app: import job reports episodes", "episode" in (st["message"] or ""), st["message"])
conn = db.connect()
check("app: imported series is in the library",
      conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 1)
check("app: imported episode is downloaded",
      conn.execute("SELECT COUNT(*) FROM episodes WHERE status='downloaded'")
      .fetchone()[0] == 1)
conn.close()
check("app: library now lists the series",
      b"class=\"cardwrap\"" in client.get("/").data)

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
