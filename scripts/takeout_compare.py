#!/usr/bin/env python3
"""What is in someone else's Google Takeout zips, and how much of it is already in the archive?  READ-ONLY.

It does not unpack anything and does not touch the archive.  A zip's table of contents already
holds each file's size and CRC-32, so this reads only that.  For every distinct photo/video it
looks for archive files of exactly the same size, and for those few candidates it reads the archive
copy once and compares its CRC-32.  Same size and same CRC-32 means the same bytes.  A file whose
size matches nothing is certainly new.

It reports, for the person's Takeout:
  * how many photos and videos it holds, and how many entries are only album copies or repeats of another entry
  * of the distinct ones: already in the archive (and in which source), matching a photo you sent
    to quarantine, or new
  * the new ones by year, and how much disk unpacking will need compared with what is free
The full list (every new file, and which archive file each duplicate matches) goes in a report file.

Not detected here: the SAME picture saved with different bytes (Google "Storage saver" re-encodes,
edited copies, resized exports).  Those are found by scripts/find_similar.py after the import.

With --extract-to STAGING it can then unpack ONLY the new files (plus the small .json sidecar files from the same
folders) into a staging folder outside originals/, laid out the way scripts/import_takeout.py expects.  Without --apply
it only says what it would unpack.  That avoids unpacking the duplicates, which can be most of a Takeout.  Items matching
something you sent to quarantine are left out unless you add --also-discarded.  It never writes inside the archive.

Usage:
  scripts/takeout_compare.py                              # zips in <archive>/takeout-zips-wife
  scripts/takeout_compare.py --zips DIR --label wife
  scripts/takeout_compare.py --archive /media/aj9/Juniper13/photo-archive
  scripts/takeout_compare.py --extract-to STAGING         # plan: what would be unpacked, and the space needed
  scripts/takeout_compare.py --extract-to STAGING --apply # do it
"""
import argparse
import glob
import os
import re
import shutil
import sqlite3
import sys
import time
import zipfile
import zlib
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from import_takeout import MEDIA_EXT, SKIP_FOLDERS  # noqa: E402

ARCHIVE_ROOT = "/media/aj9/Juniper13/photo-archive"
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".3gp", ".mp"}
YEAR_FOLDER = re.compile(r"^Photos from (\d{4})$")


def gb(n):
    return f"{n / 1e9:,.1f} GB"


def crc_of(path):
    c = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            c = zlib.crc32(chunk, c)
    return c & 0xFFFFFFFF


def source_of(path):
    parts = path.split("/")
    if parts[0] in ("photos-catchall", "exports") and len(parts) > 2:
        return parts[0] + "/" + parts[1]
    return parts[0]


def read_zips(zips):
    """Media entries: (zip, name, size, crc, folder-under-Google-Photos).  Also counts of everything else."""
    media, other, bad = [], Counter(), []
    for z in zips:
        try:
            zf = zipfile.ZipFile(z)
        except (zipfile.BadZipFile, OSError) as e:
            bad.append((z, str(e)))
            continue
        with zf:
            for i in zf.infolist():
                if i.is_dir():
                    continue
                parts = i.filename.split("/")
                ext = os.path.splitext(i.filename)[1].lower()
                if "Google Photos" not in parts:
                    other["not under Google Photos/ (other Google products)"] += 1
                    continue
                folder = parts[parts.index("Google Photos") + 1] if len(parts) > parts.index("Google Photos") + 2 else ""
                if folder in SKIP_FOLDERS:
                    other["in Trash/Bin (ignored)"] += 1
                elif ext in MEDIA_EXT:
                    media.append((os.path.basename(z), i.filename, i.file_size, i.CRC, folder))
                else:
                    other["sidecars and other files (.json etc.)"] += 1
    return media, other, bad


def safe_dest(root, name):
    """Where a zip entry goes under root, or None if its name tries to climb out of it."""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or name.startswith("/") or any(p == ".." for p in parts):
        return None
    dest = os.path.join(root, *parts)
    return dest if os.path.realpath(dest).startswith(os.path.realpath(root) + os.sep) else None


def plan_extract(zpath, chosen):
    """chosen: [(zip base name, entry name)].  Returns {zip base name: [ZipInfo]}: those entries plus every
    .json file in the same folders (sidecars; the import script matches them to photos)."""
    by_zip = defaultdict(set)
    for zb, name in chosen:
        by_zip[zb].add(name)
    out = {}
    for zb, names in by_zip.items():
        with zipfile.ZipFile(zpath[zb]) as zf:
            folders = {n.rsplit("/", 1)[0] for n in names}
            out[zb] = [i for i in zf.infolist() if not i.is_dir() and (
                i.filename in names or (i.filename.lower().endswith(".json") and i.filename.rsplit("/", 1)[0] in folders))]
    return out


def extract(zpath, plan, root):
    """Unpack the planned entries.  A file that cannot be written is recorded and the rest carry on.
    Returns (unpacked, already there, problems [(entry, why)])."""
    done = skipped = 0
    problems = []
    written = set()
    for zb, infos in plan.items():
        with zipfile.ZipFile(zpath[zb]) as zf:
            for i in infos:
                dest = safe_dest(root, i.filename)
                if dest is None:
                    problems.append((i.filename, "unsafe name"))
                    continue
                if dest in written:
                    problems.append((i.filename, f"same path as another entry already unpacked this run ({zb})"))
                    continue
                if os.path.exists(dest) and os.path.getsize(dest) == i.file_size:
                    skipped += 1
                    written.add(dest)
                    continue
                tmp = dest + ".part"
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(i) as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    if os.path.getsize(tmp) != i.file_size:
                        raise OSError("written size differs from the zip's")
                    os.replace(tmp, dest)
                except (OSError, zipfile.BadZipFile, zlib.error, RuntimeError) as e:
                    problems.append((i.filename, f"{type(e).__name__}: {e}"))
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    continue
                written.add(dest)
                try:
                    t = time.mktime(tuple(i.date_time) + (0, 0, -1))
                    os.utime(dest, (t, t))
                except (OverflowError, ValueError, OSError):
                    pass
                done += 1
                if done % 1000 == 0:
                    print(f"   {done:,} unpacked", flush=True)
    return done, skipped, problems


def load_archive(root):
    db = sqlite3.connect(f"file:{os.path.join(root, 'curator.sqlite')}?mode=ro", uri=True)
    by_size = defaultdict(list)                              # size -> [(relative path under originals, label)]
    n = 0
    for path, size in db.execute("SELECT path, size FROM files"):
        by_size[size].append((path, "archive"))
        n += 1
    nq = 0
    edits = os.path.join(root, "curator_edits.sqlite")
    if os.path.exists(edits):
        e = sqlite3.connect(f"file:{edits}?mode=ro", uri=True)
        try:
            for path, qpath, size in e.execute(
                    "SELECT path, qpath, size FROM quarantine_log WHERE role IN ('photo', 'live-video') AND restored_at IS NULL "
                    "AND purged_at IS NULL AND size IS NOT NULL"):
                by_size[size].append((qpath, "quarantine"))
                nq += 1
        except sqlite3.Error:
            pass
    last = db.execute("SELECT MAX(scanned_at) FROM files").fetchone()[0]
    return by_size, n, nq, last


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=ARCHIVE_ROOT)
    ap.add_argument("--zips", help="folder holding the takeout-*.zip files (default <archive>/takeout-zips-wife)")
    ap.add_argument("--label", default="wife", help="name used in the report file name")
    ap.add_argument("--extract-to", metavar="STAGING", help="unpack only the new files (and their sidecars) here")
    ap.add_argument("--also-discarded", action="store_true",
                    help="with --extract-to: also unpack files identical to something in quarantine")
    ap.add_argument("--apply", action="store_true", help="with --extract-to: really unpack (default is a plan only)")
    a = ap.parse_args()
    zdir = a.zips or os.path.join(a.archive, "takeout-zips-wife")
    zips = sorted(glob.glob(os.path.join(zdir, "*.zip")))
    if not zips:
        sys.exit(f"No .zip files in {zdir}. Copy the Takeout zips there first (see RUNBOOK: \"Another person's Google Takeout\").")
    originals = os.path.join(a.archive, "originals")
    print(f"Archive: {a.archive}\nZips: {len(zips)} in {zdir}  ({gb(sum(os.path.getsize(z) for z in zips))})")

    media, other, bad = read_zips(zips)
    for z, why in bad:
        print(f"  !  {z}: cannot read ({why}). Is the copy complete?  Check with: unzip -tq FILE")
    if not media:
        sys.exit("No photos or videos found under \"Google Photos/\" in those zips.")

    # distinct = one per (size, crc); the first entry in a "Photos from YYYY" or "Archive" folder is the one we describe
    groups = defaultdict(list)
    for e in media:
        groups[(e[2], e[3])].append(e)

    def rank(e):
        return (0 if YEAR_FOLDER.match(e[4]) else 1 if e[4] == "Archive" else 2, e[1])
    distinct = {k: sorted(v, key=rank) for k, v in groups.items()}
    total_bytes = sum(e[2] for e in media)
    distinct_bytes = sum(k[0] for k in distinct)
    photos = sum(1 for e in media if os.path.splitext(e[1])[1].lower() not in VIDEO_EXT)
    print(f"\nIn the zips: {len(media):,} photos and videos ({photos:,} photos, {len(media) - photos:,} videos), {gb(total_bytes)}")
    print(f"  {len(media) - len(distinct):,} of those entries are album copies or repeats of another entry in the same Takeout")
    print(f"  {len(distinct):,} distinct files ({gb(distinct_bytes)})")
    for what, n in other.most_common():
        print(f"  (not counted: {n:,} {what})")

    by_size, n_arch, n_quar, last = load_archive(a.archive)
    print(f"\nComparing with {n_arch:,} archive files and {n_quar:,} in quarantine "
          f"(reading only archive files whose size matches)...\nIndex last updated {last}. "
          f"If you have moved files since (quarantine, imports), run scripts/scan_archive.py first.")
    crc_cache = {}
    missing_on_disk = 0
    found = {}                                               # key -> (label, rel path)
    cand_total = sum(1 for k in distinct if k[0] in by_size)
    done = 0
    t0 = datetime.now()
    for key in distinct:
        size, crc = key
        for rel, label in by_size.get(size, ()):
            p = os.path.join(originals, rel)
            if rel not in crc_cache:
                try:
                    crc_cache[rel] = crc_of(p)
                except OSError:
                    crc_cache[rel] = None
                    missing_on_disk += 1
            if crc_cache[rel] == crc:
                found[key] = (label, rel)
                break
        if size in by_size:
            done += 1
            if done % 500 == 0:
                print(f"   {done:,} of {cand_total:,} size matches checked", flush=True)

    in_archive, in_quar, new = [], [], []
    for key, entries in distinct.items():
        hit = found.get(key)
        (in_archive if hit and hit[0] == "archive" else in_quar if hit else new).append((key, entries, hit))

    by_src = Counter(source_of(h[1]) for _, _, h in in_archive)
    new_bytes = sum(k[0] for k, _, _ in new)
    arch_bytes = sum(k[0] for k, _, _ in in_archive)
    print(f"\nOf the {len(distinct):,} distinct files:")
    print(f"  {len(in_archive):,} already in the archive, byte for byte ({gb(arch_bytes)})")
    for src, n in by_src.most_common():
        print(f"        {n:>7,}  in {src}")
    print(f"  {len(in_quar):,} are identical to a photo currently in quarantine (one you or this tool moved out)")
    print(f"  {len(new):,} NEW to the archive ({gb(new_bytes)})")
    if missing_on_disk:
        print(f"  ({missing_on_disk} candidate archive file(s) could not be read; those were treated as not matching)")

    years = Counter()
    for k, entries, _ in new:
        m = YEAR_FOLDER.match(entries[0][4])
        years[m[1] if m else "(album or other)"] += 1
    if years:
        print("\nNew files by year (the Takeout's own \"Photos from YYYY\" folder):")
        print("   " + "  ".join(f"{y}: {n:,}" for y, n in sorted(years.items())))

    free = shutil.disk_usage(a.archive).free
    unpacked = sum(e[2] for e in media)
    print(f"\nDisk: unpacking everything needs about {gb(unpacked)} on top of the zips; free now: {gb(free)}"
          + ("   <-- NOT ENOUGH, free some space first" if free < unpacked * 1.1 else ""))

    rpt = os.path.join(a.archive, f"takeout-compare-{a.label}-{datetime.now():%Y%m%d-%H%M%S}.txt")
    with open(rpt, "w") as f:
        f.write(f"Takeout comparison for {a.label}, {datetime.now():%Y-%m-%d %H:%M}\n\n")
        f.write(f"== NEW ({len(new)}) ==\n")
        for k, entries, _ in sorted(new, key=lambda t: t[1][0][1]):
            f.write(f"{k[0]:>12,}  {entries[0][1]}\n")
        f.write(f"\n== ALREADY IN THE ARCHIVE ({len(in_archive)}) ==\n")
        for k, entries, h in sorted(in_archive, key=lambda t: t[1][0][1]):
            f.write(f"{entries[0][1]}\n      = {h[1]}\n")
        f.write(f"\n== IDENTICAL TO A QUARANTINED FILE ({len(in_quar)}) ==\n")
        for k, entries, h in sorted(in_quar, key=lambda t: t[1][0][1]):
            f.write(f"{entries[0][1]}\n      = {h[1]}\n")
        f.write(f"\n== REPEATS WITHIN THE TAKEOUT (album copies) ==\n")
        for k, entries in distinct.items():
            if len(entries) > 1:
                f.write(f"{entries[0][1]}\n" + "".join(f"      also {e[1]}\n" for e in entries[1:]))
    print(f"\nFull lists: {rpt}")
    if not a.extract_to:
        print("Nothing was unpacked, moved or changed.")
        print("To unpack only the new files:  scripts/takeout_compare.py --extract-to STAGING   (then add --apply)")
        return

    chosen = [(e[0][0], e[0][1]) for _, e, _ in new] + ([(e[0][0], e[0][1]) for _, e, _ in in_quar] if a.also_discarded else [])
    zpath = {os.path.basename(z): z for z in zips}
    plan = plan_extract(zpath, chosen)
    n_all = sum(len(v) for v in plan.values())
    n_json = sum(1 for v in plan.values() for i in v if i.filename.lower().endswith(".json"))
    need = sum(i.file_size for v in plan.values() for i in v)
    os.makedirs(os.path.dirname(os.path.abspath(a.extract_to)), exist_ok=True)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(a.extract_to))).free
    print(f"\nUnpacking plan: {len(chosen):,} photos/videos{' (including ones matching quarantine)' if a.also_discarded else ''}"
          f" + {n_json:,} sidecar .json files = {n_all:,} files, {gb(need)}, into {a.extract_to}")
    print(f"Free space there: {gb(free)}")
    if free < need * 1.1:
        sys.exit("Not enough free space. Nothing unpacked.")
    if not a.apply:
        print("This was a plan. Add --apply to unpack. (Nothing has been written.)")
        return
    done, skipped, problems = extract(zpath, plan, a.extract_to)
    print(f"\nUnpacked {done:,} file(s); {skipped:,} were already there.")
    # check: every photo/video we meant to unpack is now present at the right size
    want = {safe_dest(a.extract_to, i.filename): i.file_size for v in plan.values() for i in v
            if not i.filename.lower().endswith(".json")}
    present = sum(1 for d, sz in want.items() if d and os.path.isfile(d) and os.path.getsize(d) == sz)
    print(f"Photos/videos present in staging: {present:,} of {len(want):,} expected"
          + ("" if present == len(want) else "   <-- NOT ALL THERE, see below"))
    if problems:
        prob = os.path.join(a.archive, f"takeout-extract-problems-{a.label}-{datetime.now():%Y%m%d-%H%M%S}.txt")
        with open(prob, "w") as f:
            for name, why in problems:
                f.write(f"{name}\t{why}\n")
        print(f"{len(problems):,} file(s) could NOT be unpacked (first few below; all in {prob}):")
        for name, why in problems[:8]:
            print(f"   {name}\n        {why}")
        print("Do not import until this is understood.")
    else:
        print("No problems. Next: scripts/import_takeout.py --staging "
              f"{a.extract_to} --dest <archive>/originals/google-wife --report <archive>/takeout-import-report-wife.txt")

if __name__ == "__main__":
    main()
