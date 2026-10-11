#!/usr/bin/env python3
"""Readiness check before clearing iCloud / Google Photos.  Read-only: it changes nothing.

For each cloud service it asks one question: is every photo and video that is in the cloud
accounted for in the archive?  "Accounted for" means one of:
  - it is in the archive (indexed in curator.sqlite), or
  - it is in quarantine because it was an exact duplicate (the kept copy is still in the archive), or
  - you chose to discard it on the near-duplicate Review page (also in quarantine).
Anything else is listed as UNACCOUNTED and the verdict is NOT READY.

  scripts/cloud_clear_check.py google            # check the Google Takeout zips against the archive
  scripts/cloud_clear_check.py google --deep     # also re-read every archive file and compare its CRC32
                                                 #   with the one inside the zip (slow: reads ~190 GB)
  scripts/cloud_clear_check.py apple --photos-json ~/photos.json
                                                 # check the Mac's Photos library (list made on the Mac,
                                                 #   see below) against the archive
  scripts/cloud_clear_check.py all --photos-json ~/photos.json

Making the Apple list (on the Mac, in Terminal, with osxphotos installed; use the system library):
  osxphotos query --json > ~/photos.json            # add  --library "/path/to/Photos Library.photoslibrary"  if unsure
Copy photos.json to the server (or a place this script can read).  The same file records albums, favourites,
keywords, captions and named people, which exist only in iCloud; this script keeps a copy in
<archive>/cloud-metadata/ so that information survives the clear-out.

It also reminds you of the things it cannot see: the offsite drive, and anything added to the cloud since
the Takeout / the Mac's download.

Locations (override with env vars): ARCHIVE=/media/aj9/Juniper13/photo-archive  BACKUP=/media/aj9/Juniper12/photo-archive-backup
Exit status: 0 = READY, 1 = NOT READY, 2 = could not run.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import zipfile
import zlib
from collections import defaultdict
from datetime import datetime

ARCHIVE = os.environ.get("ARCHIVE", "/media/aj9/Juniper13/photo-archive")
BACKUP = os.environ.get("BACKUP", "/media/aj9/Juniper12/photo-archive-backup")

MEDIA_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff", ".webp",
             ".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2",
             ".mov", ".mp4", ".m4v", ".avi", ".mts", ".3gp", ".mp"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".3gp", ".mp"}
SKIP_FOLDERS = {"Trash", "Bin"}                 # Google's own bin: not part of the library
PRIMARY = re.compile(r"^(Photos from \d{4}|Archive)$")
GP_PREFIX = "Google Photos/"


def say(*a):
    print(*a, flush=True)


class World:
    """What the archive holds: indexed files and quarantine log."""

    def __init__(self, root):
        self.root = root
        idx = os.path.join(root, "curator.sqlite")
        ed = os.path.join(root, "curator_edits.sqlite")
        if not os.path.exists(idx):
            sys.exit(f"Cannot find {idx}. Set ARCHIVE=... to the photo-archive folder.")
        db = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
        self.files = {}                          # path -> (size, kind, taken_at)
        self.last_scan = None
        for p, size, kind, taken, scanned in db.execute("SELECT path, size, kind, taken_at, scanned_at FROM files"):
            self.files[p] = (size, kind, taken)
            if scanned and (self.last_scan is None or scanned > self.last_scan):
                self.last_scan = scanned
        db.close()
        self.quar = {}                           # original path -> (size, qpath, rule, batch, purged)
        self.last_move = None
        if os.path.exists(ed):
            e = sqlite3.connect(f"file:{ed}?mode=ro", uri=True)
            try:
                for p, q, size, rule, batch, purged, moved in e.execute(
                        "SELECT path, qpath, size, rule, batch, purged_at, moved_at FROM quarantine_log "
                        "WHERE restored_at IS NULL"):
                    self.quar[p] = (size, q, rule or "", batch, purged)
                    if moved and (self.last_move is None or moved > self.last_move):
                        self.last_move = moved
            except sqlite3.OperationalError:
                pass
            e.close()

    def where(self, path, size=None):
        """('archive'|'exact-dup'|'chosen'|None, detail) for a path relative to originals/."""
        f = self.files.get(path)
        if f and (size is None or f[0] == size):
            return "archive", path
        q = self.quar.get(path)
        if q and (size is None or q[0] is None or q[0] == size):
            return ("chosen" if q[2].startswith("similar") else "exact-dup"), q[1]
        return None, None


def norm_stem(name):
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    return re.sub(r"(\s?\(\d+\)|-edited)$", "", stem)


def crc_of(path):
    c = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                return c & 0xFFFFFFFF
            c = zlib.crc32(b, c)


# ---------------------------------------------------------------- Google
def check_google(w, deep, report):
    zdir = os.path.join(w.root, "takeout-zips")
    zips = sorted(os.path.join(zdir, n) for n in os.listdir(zdir) if n.lower().endswith(".zip")) \
        if os.path.isdir(zdir) else []
    if not zips:
        say(f"GOOGLE: no zips in {zdir}. Without the Takeout zips there is nothing to compare against.")
        report.append("GOOGLE: no zips found")
        return None
    entries = []                                  # (folder, name, size, crc)
    other = skipped_bin = 0
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for i in zf.infolist():
                if i.is_dir():
                    continue
                n = i.filename
                k = n.find(GP_PREFIX)
                if k < 0:
                    other += 1
                    continue
                rel = n[k + len(GP_PREFIX):]
                folder, name = os.path.split(rel)
                ext = os.path.splitext(name)[1].lower()
                if ext not in MEDIA_EXT:
                    other += 1
                    continue
                if folder.split("/")[0] in SKIP_FOLDERS:
                    skipped_bin += 1
                    continue
                entries.append((folder, name, i.file_size, i.CRC))
    say(f"GOOGLE: {len(zips)} zip(s), {len(entries):,} photos/videos listed"
        f" (+{skipped_bin} in Google's bin ignored, {other:,} other files such as .json).")

    primary_crc = defaultdict(list)               # (size, crc) -> entries in year/Archive folders
    for e in entries:
        if PRIMARY.match(e[0].split("/")[0]):
            primary_crc[(e[2], e[3])].append(e)

    tally = defaultdict(int)
    bad, deep_jobs = [], []
    for folder, name, size, crc in entries:
        path = f"google/{folder}/{name}" if folder else f"google/{name}"
        state, detail = w.where(path, size)
        if state:
            tally[state] += 1
            deep_jobs.append((path, detail, state, crc))
            continue
        # an album copy of a photo that is itself accounted for (same size and CRC inside the zips)
        twin = None
        if not PRIMARY.match(folder.split("/")[0]):
            for t in primary_crc.get((size, crc), []):
                tp = f"google/{t[0]}/{t[1]}"
                s2, d2 = w.where(tp, size)
                if s2:
                    twin = tp
                    break
        if twin:
            tally["album copy"] += 1
            continue
        bad.append(path)
    tally["UNACCOUNTED"] = len(bad)

    changed = []
    if deep:
        say(f"  --deep: re-reading {len(deep_jobs):,} files to compare CRC32 ...")
        t0 = time.time()
        for n, (path, detail, state, crc) in enumerate(deep_jobs, 1):
            f = os.path.join(w.root, "originals", detail)
            if state == "exact-dup" or state == "chosen":
                if not os.path.isfile(f):
                    continue                       # purged: its kept copy was checked at quarantine time
            if not os.path.isfile(f):
                changed.append(f"{path}  (file missing on disk)")
            elif crc_of(f) != crc:
                changed.append(f"{path}  (contents differ from the Takeout)")
            if n % 5000 == 0:
                say(f"    {n:,} / {len(deep_jobs):,}  ({int(time.time() - t0)}s)")
        tally["differs from zip"] = len(changed)

    for k in ("archive", "exact-dup", "chosen", "album copy", "UNACCOUNTED", "differs from zip"):
        if k in tally:
            label = {"archive": "in the archive", "exact-dup": "in quarantine as exact duplicates",
                     "chosen": "in quarantine because you discarded them",
                     "album copy": "album copies of photos that are accounted for",
                     "UNACCOUNTED": "UNACCOUNTED", "differs from zip": "CHANGED since the Takeout (deep check)"}[k]
            say(f"  {tally[k]:>8,}  {label}")
    report.append(f"GOOGLE: {dict(tally)}")
    if bad:
        report.append("GOOGLE UNACCOUNTED:")
        report += ["  " + b for b in bad]
        say("  First few unaccounted:")
        for b in bad[:10]:
            say("    " + b)
    if changed:
        report.append("GOOGLE CHANGED:")
        report += ["  " + c for c in changed]
        for c in changed[:10]:
            say("    " + c)
    return not bad and not changed


# ---------------------------------------------------------------- Apple
def pick(d, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def check_apple(w, jpath, report):
    try:
        with open(jpath, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        say(f"APPLE: cannot read {jpath}: {e}")
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        say("APPLE: that file is not a list of photos (expected the output of `osxphotos query --json`).")
        return None
    keys = set(data[0].keys())
    if not (keys & {"original_filename", "filename"}) or not (keys & {"original_filesize", "filesize"}):
        say("APPLE: the list has no file name / size fields. Fields seen: " + ", ".join(sorted(keys)[:30]))
        say("       Tell Claude which fields hold the original file name and size and this can be adjusted.")
        return None

    by_size = defaultdict(list)                   # size -> [(stem, day, where)]
    vstems = set()
    for p, (size, kind, taken) in w.files.items():
        by_size[size].append((norm_stem(p), (taken or "")[:10], "archive"))
        if os.path.splitext(p)[1].lower() in VIDEO_EXT:
            vstems.add(norm_stem(p))
    for p, (size, q, rule, batch, purged) in w.quar.items():
        if size is not None:
            by_size[size].append((norm_stem(p), "", "chosen" if rule.startswith("similar") else "exact-dup"))
        if os.path.splitext(p)[1].lower() in VIDEO_EXT:
            vstems.add(norm_stem(p))

    tally = defaultdict(int)
    missing, namebad, nolive, notdl = [], [], [], []
    albums, fav, kw, persons, titled = set(), 0, 0, 0, 0
    for d in data:
        if pick(d, "intrash"):
            tally["in Recently Deleted (ignored)"] += 1
            continue
        for a in pick(d, "albums") or []:
            albums.add(a)
        fav += bool(pick(d, "favorite"))
        kw += bool(pick(d, "keywords"))
        persons += bool(pick(d, "persons"))
        titled += bool(pick(d, "title") or pick(d, "description"))
        name = pick(d, "original_filename", "filename") or ""
        size = pick(d, "original_filesize", "filesize")
        label = f"{name}  {str(pick(d, 'date') or '')[:10]}  {size} bytes  uuid {pick(d, 'uuid')}"
        if pick(d, "ismissing") or size in (None, 0):
            tally["not downloaded to this Mac (cannot be checked)"] += 1
            notdl.append(label)
            continue
        day = str(pick(d, "date") or "")[:10]
        stem = norm_stem(name)
        cands = by_size.get(size, [])
        strong = [c for c in cands if c[0] == stem]
        weak = [c for c in cands if c[1] and c[1] == day]
        if strong:
            tally["matched (same size and name)"] += 1
        elif weak:
            tally["matched (same size and day, name differs)"] += 1
        else:
            tally["UNACCOUNTED"] += 1
            missing.append(label)
            if any(norm_stem(p) == stem for p in w.files):
                namebad.append(label)
            continue
        if pick(d, "live_photo") and stem not in vstems:
            nolive.append(label)

    say(f"APPLE: {len(data):,} items in the Photos list.")
    for k, v in tally.items():
        say(f"  {v:>8,}  {k}")
    say(f"  Information that lives only in iCloud/Photos: {len(albums):,} albums, {fav:,} favourites, "
        f"{kw:,} with keywords, {persons:,} with named people, {titled:,} with a title or caption.")
    if nolive:
        say(f"  {len(nolive):,} Live Photos whose video half was not found in the archive (check these).")
    report.append(f"APPLE: {dict(tally)} albums={len(albums)} favourites={fav}")
    for title, rows in (("APPLE UNACCOUNTED", missing), ("APPLE NAME FOUND BUT SIZE DIFFERS", namebad),
                        ("APPLE LIVE PHOTO VIDEO NOT FOUND", nolive), ("APPLE NOT DOWNLOADED", notdl)):
        if rows:
            report.append(title + ":")
            report += ["  " + r for r in rows]
    for r in missing[:10]:
        say("    missing: " + r)
    # keep the metadata
    mdir = os.path.join(w.root, "cloud-metadata")
    os.makedirs(mdir, exist_ok=True)
    dest = os.path.join(mdir, f"apple-photos-{datetime.now():%Y%m%d}.json")
    if not os.path.exists(dest):
        shutil.copyfile(jpath, dest)
        say(f"  Saved a copy of the Photos list (albums, favourites, people ...) to {dest}")
    return not missing and not nolive and not notdl


# ---------------------------------------------------------------- the rest
def reminders(w):
    say("\nThings this check cannot see:")
    lb = os.path.join(BACKUP, "LAST_BACKUP.txt")
    if os.path.exists(lb):
        age = (time.time() - os.path.getmtime(lb)) / 86400
        say(f"  Backup disk: last backup {datetime.fromtimestamp(os.path.getmtime(lb)):%d %b %H:%M} ({age:.1f} days ago)")
        newest = max(filter(None, [w.last_scan, w.last_move]), default=None)
        if newest and datetime.fromtimestamp(os.path.getmtime(lb)).isoformat() < newest:
            say("  !  The archive has changed since that backup. Run scripts/backup.sh --apply first.")
    else:
        say(f"  Backup disk: no LAST_BACKUP.txt found in {BACKUP}. Is it mounted?")
    say("  [ ] The offsite drive is current (refresh it before deleting anything).")
    say("  [ ] Nothing new was added to the cloud since the Takeout / the Mac's last download.")
    say("  [ ] Google Photos / Apple backup is switched off on every device that could re-upload.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["google", "apple", "all"])
    ap.add_argument("--photos-json", help="output of `osxphotos query --json` from the Mac")
    ap.add_argument("--deep", action="store_true", help="google: re-read files and compare CRC32 (slow)")
    ap.add_argument("--archive", default=ARCHIVE)
    a = ap.parse_args()

    w = World(a.archive)
    say(f"Archive: {a.archive}  ({len(w.files):,} indexed files, {len(w.quar):,} in the quarantine log)")
    if w.last_scan:
        say(f"Index last updated {w.last_scan}. If you have moved files since, run scripts/scan_archive.py first.")
    report, results = [], {}
    if a.what in ("google", "all"):
        results["google"] = check_google(w, a.deep, report)
    if a.what in ("apple", "all"):
        if not a.photos_json:
            say("APPLE: give --photos-json (see --help).")
            results["apple"] = None
        else:
            results["apple"] = check_apple(w, a.photos_json, report)
    reminders(w)
    rpath = os.path.join(a.archive, f"cloud-clear-check-{datetime.now():%Y%m%d-%H%M%S}.txt")
    with open(rpath, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    say(f"\nFull lists: {rpath}")
    if any(v is None for v in results.values()):
        say("VERDICT: could not complete the check (see above).")
        sys.exit(2)
    if all(results.values()):
        say("VERDICT: READY on the points this script can check. Now tick the manual items above.")
        sys.exit(0)
    say("VERDICT: NOT READY. Do not delete from " + " or ".join(k for k, v in results.items() if not v) + " yet.")
    sys.exit(1)


if __name__ == "__main__":
    main()
