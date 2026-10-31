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

## Layout

```
app.py          Flask server + background download jobs
webtoon.py      webtoons.com list/viewer parsing and image downloader
db.py           SQLite schema (series, episodes, progress per profile, settings)
templates/      Library, series detail, reader, settings pages
static/         CSS + reader scroll-progress script
data/           netscroll.db (auto-created) + secret key
downloads/      per-series image folders (auto-created)
```

## Being polite & staying safe

- Requests are throttled (Settings → delays). Keep the defaults or raise them.
- This is for **personal offline backup** of content you can already read with your own
  account. Don't redistribute downloads, don't run multi-account farming (Naver bans for
  that), and respect the creators by supporting the official releases.
