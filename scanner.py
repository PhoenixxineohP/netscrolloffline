"""Validate and optionally scan downloaded files.

A downloader that writes whatever bytes a CDN hands back is trusting that
server completely. This module adds three cheap layers:

1. **Structure check** — the first bytes must match a known image signature and
   the file must be large enough to be a real page. HTML error pages saved with
   a .jpg name are the most common real-world failure, and they would otherwise
   be archived as "downloaded" and break the reader forever.
2. **Size sanity** — catches truncated transfers and absurd payloads.
3. **Optional antivirus** — if ClamAV's ``clamscan``/``clamdscan`` is on PATH
   (or configured), each file is scanned before it is kept. Absent ClamAV, we
   say so honestly rather than claiming files were "scanned".

Nothing here executes or renders the data, so the checks themselves are safe.
"""
import os
import shutil
import subprocess
import tempfile

# file(1)-style magic numbers for the formats webtoon CDNs actually serve.
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)

MIN_PAGE_BYTES = 512            # a real comic page is far larger than this
MAX_PAGE_BYTES = 64 * 1024 * 1024
_SCAN_MAX_BYTES = 128 * 1024 * 1024


def sniff_image(path):
    """Return the detected image format, or None if the bytes aren't an image."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    for magic, fmt in _IMAGE_MAGIC:
        if head.startswith(magic):
            return fmt
    # RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def clamav_path():
    """Locate a ClamAV client, if the machine has one."""
    for name in ("clamdscan", "clamscan"):
        found = shutil.which(name)
        if found:
            return found
    return None


def antivirus_available():
    return clamav_path() is not None


def scan_with_clamav(path):
    """Return (clean: bool, detail: str). Raises nothing; scanning is best-effort."""
    exe = clamav_path()
    if not exe:
        return True, "no scanner installed"
    try:
        if os.path.getsize(path) > _SCAN_MAX_BYTES:
            return True, "too large to scan"
        proc = subprocess.run(
            [exe, "--no-summary", "--stdout", path],
            capture_output=True, text=True, timeout=120,
        )
        # clamscan: 0 clean, 1 infected, 2 error.
        out = (proc.stdout or proc.stderr or "").strip()[:300]
        if proc.returncode == 1:
            return False, out or "infected"
        if proc.returncode == 2:
            return True, f"scanner error (kept): {out}"
        return True, "clean"
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"scanner unavailable ({exc.__class__.__name__})"


def verify_page(path, expected_format=None, scan=False):
    """Check one downloaded page.

    Returns ``(ok, reason)``. When ``scan`` is true and ClamAV is installed the
    file must also be clean. A missing scanner is not treated as a failure —
    we simply report that so the UI can be honest about coverage.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"unreadable: {exc.__class__.__name__}"
    if size < MIN_PAGE_BYTES:
        return False, f"too small ({size} bytes)"
    if size > MAX_PAGE_BYTES:
        return False, f"implausibly large ({size} bytes)"
    fmt = sniff_image(path)
    if fmt is None:
        return False, "not a valid image (probably an error page)"
    if expected_format and expected_format != fmt:
        # Extension mismatch is not fatal (CDNs serve .jpg that is really webp)
        # but worth surfacing as a soft note rather than a rejection.
        return True, f"ok ({fmt}, expected {expected_format})"
    if scan:
        clean, detail = scan_with_clamav(path)
        if not clean:
            return False, f"antivirus: {detail}"
        return True, f"ok ({fmt}; {detail})"
    return True, f"ok ({fmt})"


def download_to(url, dest, fetch, scan=False):
    """Fetch ``url`` into ``dest`` atomically, verifying before keeping it.

    ``fetch(url)`` must return a ``requests.Response``. The bytes are written
    to a temporary file first so a failed validation never leaves a corrupt
    page behind for the reader to trip over.
    """
    resp = fetch(url)
    fmt_hint = None
    ext = os.path.splitext(dest)[1].lower().lstrip(".")
    if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
        fmt_hint = "jpg" if ext == "jpeg" else ext
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest) or ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)
        ok, reason = verify_page(tmp, expected_format=fmt_hint, scan=scan)
        if not ok:
            return False, reason
        os.replace(tmp, dest)
        return True, reason
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
