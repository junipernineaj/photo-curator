#!/usr/bin/env python3
"""Check that every file in an old source folder is safely in the archive. READ-ONLY.

Hashes (SHA-1) every file under SOURCE and looks each one up in the scan index
(curator.sqlite). A file counts as safe if its hash is in the index, or in a parked folder
(originals/_*, such as _thumbnails-... and _previews-apple, which are not indexed but are kept).
Anything else is listed as MISSING in a report file. This script never changes or deletes anything.

Usage:
  scripts/verify_source.py /media/aj9/Juniper13/Pictures
  scripts/verify_source.py /media/aj9/Juniper12/PersonalPhotos/AGGREGATED-Photos
Options:
  --skip NAME    skip folders or files with this name (repeatable). Default skips .DS_Store,
                 Thumbs.db, and iPhoto/Photos library caches: Thumbnails, Previews, resources
  --report FILE  where to write the MISSING list (default: ~/verify-<name>.txt)
"""
import argparse
import hashlib
import os
import sqlite3
import sys

ARCHIVE_ROOT = "/media/aj9/Juniper13/photo-archive"
DEFAULT_SKIP = {".DS_Store", "Thumbs.db", "Thumbnails", "Previews", "resources", "iPod Photo Cache", "Thumbs"}


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--archive", default=ARCHIVE_ROOT)
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--report", default=None)
    a = ap.parse_args()
    skip = DEFAULT_SKIP | set(a.skip)
    src = os.path.abspath(a.source)
    if not os.path.isdir(src):
        sys.exit(f"not a folder: {src}")
    report = a.report or os.path.expanduser(f"~/verify-{os.path.basename(src.rstrip('/'))}.txt")

    db = sqlite3.connect(f"file:{os.path.join(a.archive, 'curator.sqlite')}?mode=ro", uri=True)
    indexed = {r[0] for r in db.execute("SELECT sha1 FROM files WHERE sha1 IS NOT NULL")}
    print(f"{len(indexed)} distinct hashes in the index")

    parked = None          # hashed lazily, only if something is not in the index
    def parked_hashes():
        nonlocal parked
        if parked is None:
            parked = set()
            orig = os.path.join(a.archive, "originals")
            for d in sorted(os.listdir(orig)):
                if d.startswith("_"):
                    for root, _dirs, files in os.walk(os.path.join(orig, d)):
                        for fn in files:
                            try:
                                parked.add(sha1_of(os.path.join(root, fn)))
                            except OSError:
                                pass
        return parked

    total = in_index = in_parked = 0
    missing, unreadable = [], []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if fn in skip:
                continue
            p = os.path.join(root, fn)
            total += 1
            try:
                h = sha1_of(p)
            except OSError as e:
                unreadable.append(f"{p}\t{e}")
                continue
            if h in indexed:
                in_index += 1
            elif h in parked_hashes():
                in_parked += 1
            else:
                missing.append(p)
            if total % 2000 == 0:
                print(f"  checked {total} files, {len(missing)} missing so far")
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"SOURCE {src}\n")
        for p in missing:
            f.write(f"MISSING\t{p}\n")
        for u in unreadable:
            f.write(f"UNREADABLE\t{u}\n")
    print(f"\n{total} files checked under {src}")
    print(f"  in the index:  {in_index}")
    print(f"  parked copy:   {in_parked}")
    print(f"  MISSING:       {len(missing)}")
    print(f"  unreadable:    {len(unreadable)}")
    print(f"report: {report}")
    print("SAFE TO CONSIDER RETIRING" if not missing and not unreadable
          else "NOT SAFE: look at the report first")


if __name__ == "__main__":
    main()
