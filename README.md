# NetScrollOffline

A small, local web app for **owning your webtoon reading**: it downloads webtoons.com
episodes you can legitimately read, stores them permanently as image files on your disk,
and re-serves them in a WEBTOON-style vertical "scroll" reader that saves exactly where you
stopped, per profile.

## What it is for

- The official app's "download" feature expires after 30 days (Daily Pass rentals).
- This app keeps a permanent copy of whatever your account can currently read on the web
  viewer. No paywall bypassing: locked episodes stay locked; when you unlock/read them
  (in the app or by renting on the web), run the app's **Check / Save** and any episode the
  web viewer now serves you gets archived permanently.

## Install & run

### Linux / macOS
```bash
bash run.sh
```

### Windows
Double-click `run.bat`.

Then open **http://127.0.0.1:8767** in your browser.

## First steps

1. **Settings** → paste your webtoons.com session cookie (from a browser you're logged in:
   F12 → Application → Cookies → copy the full `Cookie` header value).
2. **Library** → paste a series URL, e.g.
   `https://www.webtoons.com/en/action/auto-hunting-with-my-clones/list?title_no=5034`
3. On the series page:
   - **↻ Refresh episode list** — pulls whatever the web currently lists.
   - **⬇ Download web-readable episodes** — archives every readable episode permanently.
   - **Check availability (range)** — probes unknown episode numbers (e.g. 8–176) to see
     which ones the web viewer lets your current session read.
   - **Manual add** — if the site hides episodes from the web list entirely, create entries
     for e.g. 8–176 with URL pattern `ep-{n}` and then **Check**.
4. **Read** — the reader scrolls vertically like a webtoon; your position auto-saves per
   profile and auto-restores when you reopen the episode. The Library card shows "Resume".

## On your phone / Fire tablet

The PC is the archive; devices are light clients.

**1. Within your home Wi-Fi (streaming, instant)**
Start the app (`bash run.sh`) — it prints your PC's address, e.g. `http://192.168.1.185:8767`.
Open that on the phone. You can read and sync; nothing else needed.

**2. Anywhere + offline reading (Tailscale, free, ~5 min one-time setup)**
Offline saving and "Install app" need HTTPS, so browser rule requires it — the easy fix is Tailscale:

- On the PC: `sudo apt install tailscale` (or download from tailscale.com), then `sudo tailscale up`, sign in with any Google/Microsoft/Apple identity.
- On each phone/tablet: install the Tailscale app from the store, sign into the SAME account, done — your PC and devices are now a private network reachable anywhere.
- In a PC terminal, run once:
  ```
  sudo tailscale serve --bg https / http://localhost:8767
  ```
  It prints an address like `https://your-pc.tail1234.ts.net`. Bookmark that on your devices.
- On HTTPS addresses, use the top-right **⤓ Install app** to add NetScrollOffline to the home screen, and the series page's **📱 Device** buttons to copy episodes into phone storage. Pages then open and read with no connection, and your scroll progress syncs to the PC archive next time you're online.
- Fire Tablet: use the built-in Silk browser (or Chrome if installed). If "Install app" doesn't appear, opening the https URL and saving episodes still works.

## Deleting device copies

Saved episodes live in the browser's cache/IndexedDB on that device. Clearing the site's
browsing data ("NetScrollOffline") frees the space. The Phoenix Records archive on the PC
is never affected by any device.

## Reading flow that actually works for Daily-Pass/locked series

- Free / public episodes: download immediately.
- Locked ones: read them however your account allows (Daily Pass, coins, events). If the
  web viewer shows them while readable on your account, the app's **Save** will archive
  them permanently at that moment. Since the files land on your disk, the 30-day timer
  never applies.
- Some titles are fully app-only; the web viewer answers "Connect Error" for those, and
  the app will honestly mark them as not readable. Nothing smuggled, just accurate status.

## Sources

You can add series from more than the English WEBTOON catalogue.

- **WEBTOON, every language edition.** Paste a URL from any edition:
  `/en/`, `/es/`, `/fr/`, `/de/`, `/th/`, `/id/`, `/zh-hant/`, `/pt-br/`.
  These are genuinely separate catalogues, not translations of one list: a title
  that is Daily Pass or app-only in English may be free elsewhere, and many
  titles exist only in one edition. The **Browse other catalogues** panel on the
  Library page lists a chosen edition + genre and adds a title with one click.
- **MangaDex.** Paste a `https://mangadex.org/title/<uuid>` link. Chapters come
  from MangaDex's official public API and page images from its `@Home` network.
  This covers English scanlations of the same series plus titles WEBTOON does
  not host. No account or API key is needed for public chapters.
- **Tapas.** Paste a `https://tapas.io/series/<name>` link. Its `robots.txt`
  explicitly allows `/series/` and `/episode/` for all agents (only `/search`,
  `/rss/`, `/mature` and account pages are disallowed, and none are requested).
  Episodes are listed through the same JSON endpoint the site uses; paid or
  early-access episodes are stored as **locked** rather than retried as errors.
- **What is deliberately excluded:** mirror/aggregator sites that redistribute
  paid content without permission, and reverse-engineered private mobile app
  APIs (MangaPlus and similar). Those are the things that actually get an IP or
  account banned, and this is a personal offline archive, not a re-publication
  tool. ComicK's API currently returns 522 and Bato redirects to a closure
  notice, so neither is a working option today.

A pasted WEBTOON URL is redirected by the site to the edition that owns it, so
"does this title exist in another edition?" is answered by browsing that
edition's catalogue rather than by rewriting the locale segment of the URL.

## Rate limits and how many parallel downloads are safe

All HTTP now goes through one shared, per-host budget (`net.py`) instead of each
queue slot running its own timer. That means raising **Parallel slots** cannot
push the origin past what it tolerates — extra slots wait on the same bucket.

Measured on this machine, using `python limittest.py --raw` (which deliberately
bypasses the limiter to find the origin's own ceiling):

| Origin | Observed behaviour |
| --- | --- |
| `www.webtoons.com` (HTML pages) | ~2 rps is comfortable; 8–10 rps starts returning 429/5xx; short 48-wide bursts still succeed but are not repeatable |
| `webtoon-phinf.pstatic.net` (page images) | 24 concurrent ≈ 2.3 MiB/s (best observed); 48–64 concurrent is *slower* (0.7–1.0 MiB/s) due to connection contention |
| `api.mangadex.org` | documented **5 req/s** global; `/at-home/server` documented **40 req/min**. 60 requests at 10 concurrent returned no 429 |
| MangaDex `@Home` image hosts | fastest around 2–4 concurrent per chapter; more does not help |
| `tapas.io` (HTML) | ~1.5 rps comfortable; episode page ≈0.5 s each |
| `us-a.tapas.io` (page images) | **4 concurrent ≈ 3.4 MiB/s, the best observed**; 8 concurrent dropped to 1.7 MiB/s |

Default budgets shipped in `net.py`: 2 rps / 4 in-flight for webtoons.com, 12 rps
/ 16 in-flight for the webtoon image CDN, 3 rps for the MangaDex API, 0.6 rps for
`/at-home/server` (its own stricter quota), 1.5 rps for tapas.io and 8 rps for its
image host. They are conservative on purpose.

Practical guidance:

- **2–4 queue slots** is the sane range for a mixture of sources. The slider goes
  to 8, but HTML crawling does not get faster past a few slots, and every image
  CDN measured (WEBTOON, MangaDex, Tapas) got *slower* past ~4–8 concurrent
  requests per episode.
- Real throughput is dominated by **page count**, not slots: a 100-page episode at
  24 concurrent image fetches takes a few seconds, while the viewer-page request
  is rate-limited to ~2/s.
- If you do see 429s, the limiter pauses the **whole host** for the
  `Retry-After` period, so one blocked request cannot snowball into a ban. Lower
  the slot count or raise `img_delay` in Settings.
- `python limittest.py` (without `--raw`) reports what the app will actually do
  with its current budgets; `--raw` shows the origin's threshold. Exit code is
  non-zero if any level was rate-limited.

## Download safety

Every page can be checked before it is archived (Settings → **Download safety**):

- **Verify every saved page is a real image** (on by default in new installs):
  the bytes must match a JPEG/PNG/GIF/WebP/BMP signature and be a plausible size.
  This catches the most common real failure — an HTML error page saved under a
  `.jpg` name — which would otherwise be archived as "downloaded" and break the
  reader permanently. Failed pages mark the episode as failed so the queue's
  retry logic handles it.
- **Antivirus scan**: if ClamAV (`clamscan`/`clamdscan`) is installed, each page
  is scanned before it is kept. If it is not installed the Settings page says so
  explicitly rather than implying files were scanned.

Files are written to a `.part` file and only renamed into place after they pass,
so an interrupted or rejected download never leaves a corrupt page behind.
Nothing downloaded is ever executed or rendered by the checker.

## Download queue

Every long action — whole-series downloads, episode-list refreshes, availability checks,
exports and watchlist scans — runs through one background queue instead of blocking the
page.

- **Queue page** (top nav) shows every job with live progress. The **Parallel slots** slider
  sets how many jobs run at once (1–8). The setting is remembered.
- **Queue several series at once** from the Library page: tick the checkbox on each card
  (or **Select all**) and press **⧉ Queue selected**. Each becomes a "Download all
  episodes" job; a series already waiting or running is skipped rather than queued twice.
- **⏸ Pause queue** stops new jobs from starting while letting running ones finish;
  **▶ Resume** continues. **Cancel** works per job (a running download stops between
  episodes) and **✕ Cancel queued** clears everything that hasn't started.
- **Auto-retry** re-queues failed download/refresh/check jobs with a growing delay until
  the **Max attempts** limit; failed episodes in an otherwise-finished download are only
  re-attempted, so retries are cheap. Use **↻ Retry all failed** or a single job's
  **Retry** to try again manually.
- **Clear finished** removes done/failed/cancelled jobs; **Remove** deletes one.
- Queue preferences (slots, auto-retry, max attempts) are saved in the `settings` table
  and restored on restart. The job history itself lives in memory, so it resets when the
  server restarts.

## Bulk export for self-hosting

Everything already saved in Phoenix Records can be bundled into a single archive,
ready to drop into a self-hosted comic server (Komga, Kavita, Calibre, Tachiyomi,
or plain folders).

- **Library page** → *Download all as CBZ* or *Download all as image folders*
  exports the whole archive at once.
- **A series page** → *Export series as CBZ / folders* exports just that title.

Formats:

- **CBZ** — one `.cbz` per episode, each with a `ComicInfo.xml` (series, episode
  number, title, author, summary, page count, source URL). Folder layout is
  `Series Title/0001 - Episode Title.cbz`. Most comic servers index this directly.
- **Image folders** — plain `Series Title/0001 - Episode Title/000.jpg …` plus
  `ComicInfo.xml` per episode, for readers or scripts that want loose files.

Both include a top-level `manifest.json` listing every episode, page count and
source URL, plus a `README.txt`. The ZIP is built in the background with a
progress bar, then downloaded automatically. Archives live in `exports/` and are
cleaned automatically after a day.

## Layout

```
app.py          Flask server + background download/export jobs
webtoon.py      webtoons.com list/viewer parsing and image downloader
sources.py      source layer: WEBTOON editions + MangaDex API
net.py          shared per-host rate limiter and polite HTTP client
scanner.py      downloaded-page validation and optional antivirus scanning
limittest.py    measures real throughput and rate-limit thresholds
db.py           SQLite schema (series, episodes, progress per profile, settings)
templates/      Library, series detail, queue, reader, settings pages
static/         CSS + reader scroll-progress and queue scripts
data/           netscroll.db (auto-created) + secret key
downloads/      per-series image folders (auto-created)
exports/        temporary bulk-export ZIPs (auto-created, pruned)
tests/          offline tests (python tests/test_queue.py, python tests/test_sources.py)
```

## Being polite & staying safe

- Requests are throttled per host, and the limiter backs off the whole host when a
  server asks it to. Keep the defaults or raise the delays.
- This is for **personal offline backup** of content you can already read with your own
  account. Don't redistribute downloads, don't run multi-account farming (Naver bans for
  that), and respect the creators by supporting the official releases.
- MangaDex's API terms ask that you identify yourself (the app sends a descriptive
  User-Agent), stay under their documented limits, and credit the site and scanlation
  groups if you display the pages. Downloaded MangaDex pages keep their chapter metadata
  in `manifest.json` when exported.
- Prefer the smallest number of slots that finishes your queue. Faster is not better if
  it gets the IP blocked.
