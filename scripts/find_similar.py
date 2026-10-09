#!/usr/bin/env python3
"""Find near-duplicate photos (resized, re-saved or lightly edited copies).

Read-only on the photos. Needs the scan to have been run first (scan_archive.py).
Writes curator_similar.sqlite next to curator.sqlite; the browse app's "Similar" page
reads it. Safe to re-run: only new or changed photos are fingerprinted again.

  scripts/find_similar.py                  # all photos
  scripts/find_similar.py --limit 300      # try a small batch first
  scripts/find_similar.py --threshold 3    # stricter (0-7 bits different; default 5)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import similar  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--archive", default=os.environ.get("ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
ap.add_argument("--db", default=os.environ.get("DB"))
ap.add_argument("--thumbs", default=os.environ.get("THUMBS"))
ap.add_argument("--out", default=os.environ.get("SIM"))
ap.add_argument("--threshold", type=int, default=5, help="max differing bits of 64 (0-7)")
ap.add_argument("--workers", type=int, default=2)
ap.add_argument("--limit", type=int, default=0, help="fingerprint at most N new photos")
a = ap.parse_args()

archive = os.path.realpath(a.archive)
parent = os.path.dirname(archive)
db = a.db or os.path.join(parent, "curator.sqlite")
if not os.path.exists(db):
    sys.exit(f"No index at {db}. Run scripts/scan_archive.py first.")
similar.run(archive, db, a.thumbs or os.path.join(parent, "thumbs"),
            a.out or os.path.join(parent, "curator_similar.sqlite"),
            threshold=a.threshold, workers=a.workers, limit=a.limit)
