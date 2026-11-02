"""SQLite helpers for NetScrollOffline."""
import os, sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# NSO_DATA_DIR lets the test suite (or a portable install) point the library
# somewhere other than the project folder, so tests never touch real data.
DATA_BASE = os.environ.get("NSO_DATA_DIR") or BASE_DIR
DB_PATH = os.path.join(DATA_BASE, "data", "netscroll.db")
DOWNLOADS_DIR = os.path.join(DATA_BASE, "downloads")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title_no TEXT NOT NULL UNIQUE,
  language TEXT NOT NULL,
  genre TEXT DEFAULT '',
  slug TEXT NOT NULL,
  kind TEXT DEFAULT 'webtoon',        -- webtoon (scroll) | manga (paged)
  genre_tags TEXT DEFAULT '',         -- free-form tags, e.g. Super Hero, Action
  title TEXT NOT NULL,
  authors TEXT DEFAULT '',
  description TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  cover_kind TEXT DEFAULT 'thumb',
  series_status TEXT DEFAULT '',      -- COMPLETED | ONGOING | HIATUS | ''
  max_episode_no INTEGER DEFAULT 0,
  last_checked TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER NOT NULL REFERENCES series(id),
  episode_no INTEGER NOT NULL,
  title TEXT DEFAULT '',
  url TEXT NOT NULL,
  known INTEGER DEFAULT 0,        -- 1 = listed on the site / confirmed by a check
  status TEXT DEFAULT 'unknown',  -- unknown | available | locked | downloaded | error
  error TEXT DEFAULT '',
  image_count INTEGER DEFAULT 0,
  downloaded_at TEXT DEFAULT '',
  UNIQUE(series_id, episode_no)
);

CREATE TABLE IF NOT EXISTS progress (
  profile_id INTEGER NOT NULL,
  series_id INTEGER NOT NULL,
  episode_no INTEGER NOT NULL,
  scroll_frac REAL DEFAULT 0.0,
  scroll_px INTEGER DEFAULT 0,
  updated_at TEXT DEFAULT '',
  PRIMARY KEY (profile_id, series_id, episode_no)
);

CREATE TABLE IF NOT EXISTS watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,          -- the series name you asked to track
  notes TEXT DEFAULT '',
  match_status TEXT DEFAULT 'unmatched',  -- unmatched | matched | not_found
  title_no TEXT DEFAULT '',
  title TEXT DEFAULT '',
  language TEXT DEFAULT 'en',
  genre TEXT DEFAULT '',
  slug TEXT DEFAULT '',
  ep_total_est INTEGER DEFAULT 0,
  unlock_type TEXT DEFAULT 'unknown', -- unknown | free | daily_pass | coins | other
  last_checked TEXT DEFAULT ''
);
"""


# How long a connection waits for a competing writer before raising
# "database is locked". Job threads all write progress, so this matters as soon
# as the queue runs more than one slot.
BUSY_TIMEOUT_MS = 15000


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers and one writer proceed at the same time instead of
    # serialising every job thread on a single write lock.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def setting(conn, key: str, default: str = "") -> str:
    """Read one setting using an already-open connection."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        # Migrations for DBs created before a given column existed
        series_cols = {r["name"] for r in conn.execute("PRAGMA table_info(series)")}
        for col, ddl in (("kind", "TEXT DEFAULT 'webtoon'"),
                         ("genre_tags", "TEXT DEFAULT ''"),
                         ("genre", "TEXT DEFAULT ''"),
                         ("source", "TEXT DEFAULT 'webtoon'"),
                         ("source_ref", "TEXT DEFAULT ''")):
            if col not in series_cols:
                conn.execute(f"ALTER TABLE series ADD COLUMN {col} {ddl}")
        # Default profile + settings
        if not conn.execute("SELECT 1 FROM profiles LIMIT 1").fetchone():
            conn.execute("INSERT INTO profiles (name) VALUES ('reader')")
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("webtoon_cookie", "")
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("check_delay", "1.0")
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("img_delay", "0.35")
        )
        for key, value in (
            ("scan_downloads", "0"),      # verify image structure on every save
            ("scan_antivirus", "0"),      # also run ClamAV when it is installed
            ("source_prefer", "webtoon"), # default catalogue for new series
        ):
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.close()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def episode_dir(series_title_no: str, episode_no: int) -> str:
    return os.path.join(DOWNLOADS_DIR, series_title_no, "ep_" + f"{episode_no:04d}")
