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
  --recheck REPORT  no hashing: re-sort an existing report, separating junk (library caches, GoPro
                 proxies, broken shortcuts) from metadata (.aae, AlbumData2.xml) and REAL files
  --report FILE  where to write the MISSING list (default: ~/verify-<name>.txt)
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
from collections import Counter

ARCHIVE_ROOT = "/media/aj9/Juniper13/photo-archive"
DEFAULT_SKIP = {".DS_Store", "Thumbs.db", "Thumbnails", "Previews", "resources", "iPod Photo Cache", "Thumbs"}


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LIB = re.compile(r"\.(photoslibrary|aplibrary|photolibrary)/", re.I)
INTERNAL_DIRS = re.compile(r"/(private|database|scopes|internal|Backup)/")
INTERNAL_FILES = {".ipspot_update", "Projects.db", "ProjectDBVersion.plist", "PkgInfo", "Recents.plist"}
METADATA = re.compile(r"(\.aae$|AlbumData2?\.xml$)", re.I)


def classify(path, kind="MISSING", names=frozenset()):
    """Sort a MISSING/UNREADABLE entry: 'junk' (safe to ignore), 'metadata' (small edit/album
    information worth keeping a copy of) or 'real' (could be a photo or video: must be looked at)."""
    name = os.path.basename(path)
    if kind == "UNREADABLE":
        return "junk" if os.path.islink(path) and not os.path.exists(path) else "real"
    if METADATA.search(name):
        return "metadata"
    if LIB.search(path) and (INTERNAL_DIRS.search(path) or name in INTERNAL_FILES):
        return "junk"
    if name in INTERNAL_FILES or "/Snagit/" in path or "/Photo Booth Library/" in path:
        return "junk"
    stem, ext = os.path.splitext(path)
    if ext.lower() in (".lrv", ".thm"):       # GoPro proxy video / thumbnail beside the real clip
        for e in (".MP4", ".mp4", ".Mp4"):
            if os.path.exists(stem + e):
                return "junk"
        if (os.path.basename(stem) + ".mp4").lower() in names:   # the real clip is in the archive
            return "junk"
    return "real"


def verdict(entries, names=frozenset()):
    """entries: [(kind, path)].  Prints a summary and returns the verdict line."""
    groups = {"junk": [], "metadata": [], "real": []}
    for kind, p in entries:
        groups[classify(p, kind, names)].append(p)
    print(f"  ignorable (library caches/databases, broken shortcuts, GoPro proxies, Snagit): {len(groups['junk'])}")
    print(f"  metadata (Photos .aae edit files, AlbumData2.xml album lists):                 {len(groups['metadata'])}")
    print(f"  REAL (could be photos or videos):                                              {len(groups['real'])}")
    for p in groups["real"][:30]:
        print(f"     {p}")
    for p in groups["metadata"][:30]:
        print(f"     (metadata) {p}")
    if groups["real"]:
        return "NOT SAFE: the REAL files above are not in the archive"
    if groups["metadata"]:
        return "SAFE FOR PHOTOS AND VIDEOS; the metadata files listed are not in the archive: keep the library packages"
    return "SAFE TO CONSIDER RETIRING"


def recheck(report, archive=ARCHIVE_ROOT):
    entries = []
    for line in open(report, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if parts[0] in ("MISSING", "UNREADABLE") and len(parts) > 1:
            entries.append((parts[0], parts[1].split("[Errno")[0]))
    print(f"{len(entries)} entries in {report}")
    db = sqlite3.connect(f"file:{os.path.join(archive, 'curator.sqlite')}?mode=ro", uri=True)
    names = {os.path.basename(r[0]).lower() for r in db.execute("SELECT path FROM files WHERE lower(ext) IN ('mp4', '.mp4')")}
    print(verdict(entries, names))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?")
    ap.add_argument("--archive", default=ARCHIVE_ROOT)
    ap.add_argument("--skip", action="append", default=[])
    ap.add_argument("--report", default=None)
    ap.add_argument("--recheck", metavar="REPORT",
                    help="no hashing: re-sort an existing report into ignorable / metadata / REAL")
    a = ap.parse_args()
    if a.recheck:
        return recheck(a.recheck, a.archive)
    if not a.source:
        ap.error("give a source folder (or --recheck REPORT)")
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
    names = {os.path.basename(r[0]).lower() for r in db.execute("SELECT path FROM files WHERE lower(ext) IN ('mp4', '.mp4')")}
    print(verdict([("MISSING", p) for p in missing] +
                  [("UNREADABLE", u.split("\t")[0]) for u in unreadable], names))


if __name__ == "__main__":
    main()
