#!/usr/bin/env python3
"""Scan the photo archive into a SQLite index. READ-ONLY on the photos.

Walks the archive, skips anything it should not index, and records one row per
photo/video: size, SHA-1, capture date (and where the date came from),
dimensions, camera, Live Photo pairing key, and whether a sidecar exists.

It never modifies, moves or deletes a photo. It only writes the SQLite file.

Needs:  python3 (stdlib only) and the `exiftool` program.
        sudo apt install libimage-exiftool-perl

Usage:
  scripts/scan_archive.py                      # scan default archive
  scripts/scan_archive.py --limit 200          # try a small batch first
  scripts/scan_archive.py --report             # print the summary only
  ARCHIVE=/some/path DB=/some/index.sqlite scripts/scan_archive.py

Re-running is cheap: files whose path, size and mtime are unchanged are skipped.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

DEFAULT_ARCHIVE = "/media/aj9/Juniper13/photo-archive/originals"

PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff",
             ".webp", ".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".3gp"}
SIDECAR_EXT = {".xmp", ".aae", ".thm"}
# Folders that are never indexed: anything starting with "_" (our parking
# folders) and the internals of Apple/Aperture library packages.
SKIP_DIR_SUFFIX = (".photoslibrary", ".aplibrary", ".migratedphotolibrary",
                   ".photolibrary", ".iphotolibrary")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,        -- relative to the archive root
  kind TEXT NOT NULL,               -- photo | video
  ext TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  sha1 TEXT,
  taken_at TEXT,                    -- ISO 8601, local time, no zone
  date_source TEXT,                 -- exif | folder | mtime
  width INTEGER, height INTEGER,
  make TEXT, model TEXT,
  content_id TEXT,                  -- Apple Live Photo content identifier
  pair_key TEXT,                    -- shared by a Live Photo's still and video
  has_sidecar INTEGER NOT NULL DEFAULT 0,
  scanned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_sha1 ON files(sha1);
CREATE INDEX IF NOT EXISTS idx_files_taken ON files(taken_at);
CREATE INDEX IF NOT EXISTS idx_files_pair ON files(pair_key);
"""

FOLDER_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[_\-.]?(0[1-9]|1[0-2])[_\-.]?(0[1-9]|[12]\d|3[01])(?!\d)")
FOLDER_YEAR_MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})[_\-.](0[1-9]|1[0-2])(?!\d)")
FOLDER_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def parse_exif_date(value):
    """exiftool gives 'YYYY:MM:DD HH:MM:SS' (maybe with zone/fractions)."""
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", value)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if y < 1900 or mo == 0 or d == 0:       # 0000:00:00 etc. means "unset"
        return None
    try:
        return datetime(y, mo, d, h, mi, s).isoformat()
    except ValueError:
        return None


def date_from_path(rel_path):
    """Guess a date from folder names like 2013_02_10, 2014-11, or 2016."""
    parts = rel_path.split(os.sep)[:-1]
    for part in reversed(parts):
        m = FOLDER_DATE.search(part)
        if m:
            try:
                return datetime(int(m[1]), int(m[2]), int(m[3])).isoformat()
            except ValueError:
                pass
        m = FOLDER_YEAR_MONTH.search(part)
        if m:
            return datetime(int(m[1]), int(m[2]), 1).isoformat()
        m = FOLDER_YEAR.search(part)
        if m:
            return datetime(int(m[1]), 1, 1).isoformat()
    return None


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_exiftool(abs_paths):
    """Return {abs_path: tags} for a batch of files. Never raises on bad files."""
    if not abs_paths:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                     encoding="utf-8") as arg:
        arg.write("\n".join(abs_paths) + "\n")
        argfile = arg.name
    try:
        cmd = ["exiftool", "-json", "-n", "-q", "-m", "-charset", "filename=utf8",
               "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
               "-ImageWidth", "-ImageHeight", "-Make", "-Model",
               "-ContentIdentifier", "-@", argfile]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
        try:
            rows = json.loads(res.stdout) if res.stdout.strip() else []
        except json.JSONDecodeError:
            rows = []
        return {r.get("SourceFile"): r for r in rows}
    finally:
        os.unlink(argfile)


def walk_archive(root):
    """Yield (abs_path, rel_path, has_sidecar) for every indexable file."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith("_")
                             and not d.lower().endswith(SKIP_DIR_SUFFIX))
        names = set(filenames)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in PHOTO_EXT and ext not in VIDEO_EXT:
                continue
            has_sidecar = (name + ".xmp" in names
                           or os.path.splitext(name)[0] + ".xmp" in names
                           or os.path.splitext(name)[0] + ".aae" in names
                           or os.path.splitext(name)[0] + ".AAE" in names)
            ap = os.path.join(dirpath, name)
            yield ap, os.path.relpath(ap, root), has_sidecar


def build_pair_keys(db):
    """Find Live Photo pairs (a still and its short video).

    1. Files sharing an Apple content identifier, if at least two share it.
    2. Otherwise: same folder + same name stem, one photo and one video.
    A file with a content identifier that nobody else shares still gets a chance
    at step 2 (the still and video do not always both carry the identifier)."""
    db.execute("UPDATE files SET pair_key = NULL")
    db.execute("UPDATE files SET pair_key = 'cid:' || content_id WHERE content_id IN ("
               "SELECT content_id FROM files WHERE content_id IS NOT NULL "
               "AND content_id != '' GROUP BY content_id HAVING COUNT(*) >= 2)")
    rows = db.execute("SELECT id, path, kind FROM files WHERE pair_key IS NULL").fetchall()
    groups = {}
    for fid, path, kind in rows:
        stem = os.path.splitext(path)[0].lower()
        groups.setdefault(stem, []).append((fid, kind))
    for stem, members in groups.items():
        kinds = {k for _, k in members}
        if kinds == {"photo", "video"}:
            for fid, _ in members:
                db.execute("UPDATE files SET pair_key = ? WHERE id = ?",
                           ("stem:" + stem, fid))
    db.commit()


def report(db):
    q = lambda sql: db.execute(sql).fetchall()
    total = q("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files")[0]
    print(f"\nIndexed files: {total[0]}   total size: {total[1] / 1e9:.2f} GB")
    print("\nBy type:")
    for ext, n in q("SELECT ext, COUNT(*) FROM files GROUP BY ext ORDER BY 2 DESC"):
        print(f"  {ext:8} {n}")
    print("\nWhere the date came from:")
    for src, n in q("SELECT COALESCE(date_source,'none'), COUNT(*) FROM files "
                    "GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {src:8} {n}")
    print("\nBy year taken:")
    for yr, n in q("SELECT substr(taken_at,1,4), COUNT(*) FROM files "
                   "WHERE taken_at IS NOT NULL GROUP BY 1 ORDER BY 1"):
        print(f"  {yr}  {n}")
    dup = q("SELECT COUNT(*), COALESCE(SUM(c-1),0) FROM ("
            "SELECT COUNT(*) c FROM files WHERE sha1 IS NOT NULL "
            "GROUP BY sha1, size HAVING c > 1)")[0]
    print(f"\nExact-duplicate groups: {dup[0]}   surplus copies: {dup[1]}")
    live = q("SELECT COUNT(DISTINCT pair_key) FROM files WHERE pair_key IS NOT NULL")[0][0]
    print(f"Live Photo pairs found: {live}")
    side = q("SELECT COUNT(*) FROM files WHERE has_sidecar = 1")[0][0]
    print(f"Files with an XMP/AAE sidecar: {side}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=os.environ.get("ARCHIVE", DEFAULT_ARCHIVE))
    ap.add_argument("--db", default=os.environ.get("DB"))
    ap.add_argument("--limit", type=int, default=0, help="stop after N new/changed files")
    ap.add_argument("--no-hash", action="store_true", help="skip SHA-1 (faster, no dup check)")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--report", action="store_true", help="only print the summary")
    args = ap.parse_args()

    archive = os.path.abspath(args.archive)
    if not os.path.isdir(archive):
        sys.exit(f"Archive folder not found: {archive}")
    dbpath = args.db or os.path.join(os.path.dirname(archive), "curator.sqlite")
    if not args.report and subprocess.run(["which", "exiftool"],
                                          capture_output=True).returncode != 0:
        sys.exit("exiftool not found. Install it:  sudo apt install libimage-exiftool-perl")

    db = sqlite3.connect(dbpath)
    db.executescript(SCHEMA)
    print(f"Archive: {archive}\nIndex:   {dbpath}")

    if not args.report:
        known = {p: (s, m) for p, s, m in
                 db.execute("SELECT path, size, mtime_ns FROM files")}
        seen = set()
        batch, done, skipped, t0 = [], 0, 0, time.time()

        def flush():
            nonlocal done
            if not batch:
                return
            tags = run_exiftool([b[0] for b in batch])
            for abs_path, rel, has_side, st in batch:
                t = tags.get(abs_path, {})
                ext = os.path.splitext(rel)[1].lower()
                kind = "video" if ext in VIDEO_EXT else "photo"
                taken = (parse_exif_date(t.get("DateTimeOriginal"))
                         or parse_exif_date(t.get("CreateDate"))
                         or parse_exif_date(t.get("MediaCreateDate")))
                source = "exif" if taken else None
                if not taken:
                    taken = date_from_path(rel)
                    source = "folder" if taken else None
                if not taken:
                    taken = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                    source = "mtime"
                try:
                    digest = None if args.no_hash else sha1_of(abs_path)
                except OSError as e:
                    print(f"  could not read {rel}: {e}")
                    continue
                w, h = t.get("ImageWidth"), t.get("ImageHeight")
                db.execute(
                    "INSERT INTO files (path,kind,ext,size,mtime_ns,sha1,taken_at,"
                    "date_source,width,height,make,model,content_id,has_sidecar,scanned_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(path) DO UPDATE SET kind=excluded.kind,ext=excluded.ext,"
                    "size=excluded.size,mtime_ns=excluded.mtime_ns,sha1=excluded.sha1,"
                    "taken_at=excluded.taken_at,date_source=excluded.date_source,"
                    "width=excluded.width,height=excluded.height,make=excluded.make,"
                    "model=excluded.model,content_id=excluded.content_id,"
                    "has_sidecar=excluded.has_sidecar,scanned_at=excluded.scanned_at",
                    (rel, kind, ext, st.st_size, st.st_mtime_ns, digest, taken, source,
                     int(w) if isinstance(w, (int, float)) else None,
                     int(h) if isinstance(h, (int, float)) else None,
                     t.get("Make"), t.get("Model"),
                     str(t["ContentIdentifier"]) if t.get("ContentIdentifier") else None,
                     int(has_side), datetime.now().isoformat(timespec="seconds")))
                done += 1
            db.commit()
            batch.clear()
            print(f"  scanned {done} files ({time.time() - t0:.0f}s)", flush=True)

        for abs_path, rel, has_side in walk_archive(archive):
            seen.add(rel)
            st = os.stat(abs_path)
            if known.get(rel) == (st.st_size, st.st_mtime_ns):
                skipped += 1
                continue
            batch.append((abs_path, rel, has_side, st))
            if len(batch) >= args.batch:
                flush()
            if args.limit and done + len(batch) >= args.limit:
                break
        flush()
        # forget files that have disappeared from disk (index only; never touches photos)
        if not args.limit:
            gone = [p for p in known if p not in seen]
            for p in gone:
                db.execute("DELETE FROM files WHERE path = ?", (p,))
            db.commit()
            if gone:
                print(f"  removed {len(gone)} vanished files from the index")
        print(f"Done. New or changed: {done}, unchanged and skipped: {skipped}")
        build_pair_keys(db)

    report(db)
    db.close()


if __name__ == "__main__":
    main()
