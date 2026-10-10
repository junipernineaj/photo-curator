#!/usr/bin/env python3
"""Park generated thumbnails out of the index (move, never delete).

Some old folders hold tool-made small copies of photos, named like J240x240-09561.jpg
(width x height in the name). They are not photos in their own right: they clutter the
timeline and make "similar" groups with their own originals. This script MOVES the clearly
small ones into a folder whose name starts with "_", which the scanner skips:

    originals/photos-catchall/PicturesPreFreya/JPEG/J96x96-07401.jpg
 -> originals/_thumbnails-PicturesPreFreya/JPEG/J96x96-07401.jpg

A file is parked only if ALL of these hold: it is under --source, its name looks like
J<w>x<h>-<n>.jpg, both sides are at most --max-side pixels, and it has no camera make.
Anything larger, with camera data, or of unknown size stays where it is for review.

Default is a DRY RUN. With --apply it renames files on the same disk (no copy, nothing
deleted, nothing overwritten) and writes a manifest, so --undo can put them back.
After --apply, re-run scripts/scan_archive.py (it forgets the moved files) and
find_similar.py. Folders starting with "_" are never indexed, but are still backed up.

Usage:
  scripts/park_thumbnails.py                 # dry run
  scripts/park_thumbnails.py --apply
  scripts/park_thumbnails.py --undo          # move everything in the manifest back
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import Counter

ARCHIVE_ROOT = "/media/aj9/Juniper13/photo-archive"
NAME = re.compile(r"^J\d+x\d+-\d+\.jpe?g$", re.I)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=ARCHIVE_ROOT)
    ap.add_argument("--source", default="photos-catchall/PicturesPreFreya",
                    help="folder under originals/ to look in")
    ap.add_argument("--dest", default="_thumbnails-PicturesPreFreya",
                    help="folder under originals/ to park into (must start with _)")
    ap.add_argument("--max-side", type=int, default=500)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    a = ap.parse_args()
    if not a.dest.startswith("_"):
        sys.exit("--dest must start with '_' so the scanner skips it")
    originals = os.path.join(a.archive, "originals")
    manifest = a.manifest or os.path.join(a.archive, f"park-manifest{a.dest}.tsv")

    if a.undo:
        if not os.path.exists(manifest):
            sys.exit(f"no manifest at {manifest}")
        back = skipped = 0
        for line in open(manifest, encoding="utf-8"):
            old, new = line.rstrip("\n").split("\t")
            src, dst = os.path.join(originals, new), os.path.join(originals, old)
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
                back += 1
            else:
                skipped += 1
        print(f"moved back {back}, skipped {skipped}. Re-run scripts/scan_archive.py.")
        return

    db = sqlite3.connect(f"file:{os.path.join(a.archive, 'curator.sqlite')}?mode=ro", uri=True)
    prefix = a.source.rstrip("/") + "/"
    rows = db.execute("SELECT path, width, height, make, size FROM files "
                      "WHERE kind='photo' AND substr(path,1,?)=?", (len(prefix), prefix)).fetchall()
    park, kept = [], Counter()
    for path, w, h, make, size in rows:
        if not NAME.match(os.path.basename(path)):
            kept["other names (real photos)"] += 1
        elif not w or not h:
            kept["J-name, unknown size"] += 1
        elif make:
            kept["J-name, has camera data"] += 1
        elif max(w, h) > a.max_side:
            kept[f"J-name, over {a.max_side}px"] += 1
        else:
            park.append(path)
    print(f"{len(rows)} photos under {a.source}")
    print(f"  to park: {len(park)}")
    for k, v in sorted(kept.items()):
        print(f"  stay:    {v:6d}  {k}")
    for p in park[:5]:
        print("  e.g.", p)
    if not a.apply:
        print("\nDRY RUN: nothing moved. Add --apply to move them.")
        return
    moved = clash = 0
    with open(manifest, "a", encoding="utf-8") as mf:
        for p in park:
            new = os.path.join(a.dest, p[len(prefix):])
            src, dst = os.path.join(originals, p), os.path.join(originals, new)
            if not os.path.exists(src) or os.path.exists(dst):
                clash += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            mf.write(f"{p}\t{new}\n")
            moved += 1
    print(f"moved {moved}, skipped {clash} (missing or already there). Manifest: {manifest}")
    print("Next: scripts/scan_archive.py, then find_similar.py")


if __name__ == "__main__":
    main()
