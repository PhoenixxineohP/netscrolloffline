"""Adopt downloads that already exist on disk into the library database.

Useful when the pages are on disk but the app shows an empty library — for
example after copying an old install's ``downloads/`` folder without its
``data/`` folder, or after moving the app to a new PC.

    python import_downloads.py                    # scan ./downloads
    python import_downloads.py /old/app           # scan <that>/downloads
    python import_downloads.py /old/app/downloads --no-lookup
    python import_downloads.py --dry-run          # only report what it finds

``--no-lookup`` skips the network and leaves placeholder titles (safe offline).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import localimport


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=db.DOWNLOADS_DIR,
                    help="downloads folder, app folder, or one series folder "
                         f"(default: {db.DOWNLOADS_DIR})")
    ap.add_argument("--no-lookup", action="store_true",
                    help="do not contact the sites for titles and covers")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be imported and change nothing")
    args = ap.parse_args(argv)

    db.init_db()
    try:
        found = localimport.scan(args.path)
    except localimport.ImportError_ as exc:
        print(f"error: {exc}")
        return 2

    if not found:
        print(f"No downloaded episodes found under {args.path}")
        print("Expected a layout like: downloads/<series>/ep_0001/*.jpg")
        return 1

    total_eps = sum(len(s["episodes"]) for s in found)
    total_bytes = sum(e["bytes"] for s in found for e in s["episodes"])
    print(f"Found {len(found)} series, {total_eps} episode(s), "
          f"{total_bytes / 1048576:.1f} MB")
    for item in found:
        first = item["episodes"][0]["no"]
        last = item["episodes"][-1]["no"]
        print(f"  {item['key']:<24} {len(item['episodes']):>4} episode(s) "
              f"(ep {first}–{last})")

    if args.dry_run:
        print("\nDry run — nothing was written.")
        return 0

    def progress(index, total, title):
        print(f"  [{index}/{total}] {title}", flush=True)

    stats = localimport.adopt(found, lookup=not args.no_lookup,
                              progress=progress)
    print(f"\nImported {stats['new_episodes']} new episode(s) across "
          f"{stats['new_series']} new series.")
    if stats["partial"]:
        print(f"{stats['partial']} episode(s) look incomplete (.part files) and "
              "were left as 'unknown' so they can be re-downloaded.")
    if not args.no_lookup:
        print(f"Resolved titles for {stats['resolved']}/{stats['series']} series.")
    print("Open the app and the Library page should now list them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
