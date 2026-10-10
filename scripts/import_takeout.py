#!/usr/bin/env python3
"""Move an unpacked Google Takeout into the archive (originals/google/).

Unpack the Takeout zips into a staging folder OUTSIDE originals/ first, for example
photo-archive/takeout-unpacked/Takeout/Google Photos/... . This script then:

  * matches each photo/video to its JSON sidecar (Google names them
    "<file>.supplemental-metadata.json", truncates long names, numbers duplicates as
    "(1)", and gives "-edited" copies no sidecar of their own);
  * MOVES the media into originals/google/<same folder>/ (a rename on the same disk,
    so no second 188 GB), and moves its sidecar next to it under the canonical name
    "<file>.supplemental-metadata.json", where scan_archive.py reads the capture date;
  * skips album copies that are byte-identical to a photo in a "Photos from YYYY" or
    "Archive" folder (it leaves them in staging; nothing is deleted).

It never deletes anything and never overwrites. Default is a DRY RUN: nothing moves
until you pass --apply. Re-running is safe: files already moved are noticed and skipped.

Usage:
  scripts/import_takeout.py                    # dry run: counts, examples, report file
  scripts/import_takeout.py --apply            # do it
  scripts/import_takeout.py --staging DIR --dest DIR --report FILE
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter

ARCHIVE_ROOT = "/media/aj9/Juniper13/photo-archive"
MEDIA_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff", ".webp",
             ".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2",
             ".mov", ".mp4", ".m4v", ".avi", ".mts", ".3gp", ".mp"}
SUP = ".supplemental-metadata"
PRIMARY_FOLDER = re.compile(r"^(Photos from \d{4}|Archive)$")
SKIP_FOLDERS = {"Trash", "Bin"}


def split_dup(name):
    """'IMG(1).jpg' -> ('IMG.jpg', '(1)'); no number -> (name, '')."""
    stem, ext = os.path.splitext(name)
    m = re.match(r"^(.*)(\(\d+\))$", stem)
    return (m[1] + ext, m[2]) if m else (name, "")


def base_names(name):
    """The file itself, then the original it was edited from (edited copies share a sidecar)."""
    out = [name]
    stem, ext = os.path.splitext(name)
    if stem.lower().endswith("-edited"):
        out.append(stem[: -len("-edited")] + ext)
    return out


def exact_candidates(name):
    out = []
    for base in base_names(name):
        base2, dup = split_dup(base)
        for c in (base + SUP + ".json",          # new style
                  base2 + SUP + dup + ".json",   # new style, numbered duplicate
                  base2 + dup + ".json",         # old style, numbered duplicate: IMG.jpg(1).json
                  base + ".json"):               # old style
            if c not in out:
                out.append(c)
    return out


def title_ok(data, base):
    """Loose check that a sidecar's title belongs to this file (used for truncated names)."""
    title = (data.get("title") or "").replace("/", "_")
    ts = os.path.splitext(title)[0].lower()
    bs = os.path.splitext(base)[0].lower()
    return bool(ts) and (bs.startswith(ts) or ts.startswith(bs))


def find_sidecar(name, jsons, load):
    """Return the sidecar file name in this folder for `name`, or None.

    jsons: set of .json names in the folder; load(name) -> parsed dict or None."""
    for c in exact_candidates(name):
        if c in jsons:
            return c
    # Truncated names: the sidecar stem is a prefix of "<file>.supplemental-metadata".
    for base in base_names(name):
        full = base + SUP
        best = None
        for j in jsons:
            s = j[:-5]
            if len(s) >= 20 and len(s) < len(full) and full.startswith(s):
                if best is None or len(s) > len(best[0]):
                    best = (s, j)
        if best:
            data = load(best[1])
            if data is not None and title_ok(data, base):
                return best[1]
    return None


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_google_photos_dir(staging):
    for cand in (os.path.join(staging, "Takeout", "Google Photos"),
                 os.path.join(staging, "Google Photos")):
        if os.path.isdir(cand):
            return cand
    sys.exit(f"No 'Takeout/Google Photos' folder under {staging}. Unpack the zips there first.")


def plan(gp):
    """Walk the staging tree. Returns (items, stats, lists)."""
    items = []          # dicts: src, folder, name, sidecar(path or None), primary(bool)
    stats = Counter()
    lists = {"no_sidecar": [], "orphan_json": [], "other_files": []}
    cache = {}
    for dirpath, dirnames, filenames in os.walk(gp):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_FOLDERS)
        folder = os.path.relpath(dirpath, gp)
        if folder == ".":
            folder = ""
        primary = bool(PRIMARY_FOLDER.match(os.path.basename(folder)))
        jsons = {n for n in filenames if n.lower().endswith(".json")}
        media = [n for n in filenames if os.path.splitext(n)[1].lower() in MEDIA_EXT
                 and not n.startswith(".")]
        for n in filenames:
            if n not in jsons and n not in media:
                lists["other_files"].append(os.path.join(folder, n))

        def load(jn, dirpath=dirpath):
            p = os.path.join(dirpath, jn)
            if p not in cache:
                try:
                    with open(p, encoding="utf-8") as f:
                        cache[p] = json.load(f)
                except (OSError, ValueError):
                    cache[p] = None
            return cache[p]

        used = set()
        for n in sorted(media):
            sc = find_sidecar(n, jsons, load)
            if sc:
                used.add(sc)
            else:
                lists["no_sidecar"].append(os.path.join(folder, n))
            items.append({"src": os.path.join(dirpath, n), "folder": folder, "name": n,
                          "sidecar": os.path.join(dirpath, sc) if sc else None,
                          "primary": primary})
        for jn in sorted(jsons - used):
            if jn != "metadata.json":
                lists["orphan_json"].append(os.path.join(folder, jn))
        if "metadata.json" in jsons:                # album/folder description
            stats["folder_metadata"] += 1
    return items, stats, lists


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging", default=os.path.join(ARCHIVE_ROOT, "takeout-unpacked"))
    ap.add_argument("--dest", default=os.path.join(ARCHIVE_ROOT, "originals", "google"))
    ap.add_argument("--report", default=os.path.join(ARCHIVE_ROOT, "takeout-import-report.txt"))
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    args = ap.parse_args()

    gp = find_google_photos_dir(os.path.abspath(args.staging))
    dest = os.path.abspath(args.dest)
    print(f"Staging: {gp}\nDest:    {dest}\nMode:    {'APPLY' if args.apply else 'dry run'}")

    items, stats, lists = plan(gp)
    stats["media_found"] = len(items)
    stats["with_sidecar"] = sum(1 for i in items if i["sidecar"])

    # Album copies: skip when byte-identical to a photo in a year/Archive folder.
    primary_index = {}
    for i in items:
        if i["primary"]:
            try:
                primary_index.setdefault((i["name"], os.path.getsize(i["src"])), []).append(i["src"])
            except OSError:
                pass
    # ... and what earlier runs already moved into the destination, so a re-run still
    # recognises leftover album copies.
    for dirpath, _dirs, files in os.walk(dest):
        if PRIMARY_FOLDER.match(os.path.basename(dirpath)):
            for n in files:
                if os.path.splitext(n)[1].lower() in MEDIA_EXT:
                    p = os.path.join(dirpath, n)
                    primary_index.setdefault((n, os.path.getsize(p)), []).append(p)
    todo, skipped_dupes = [], []
    for i in items:
        if not i["primary"]:
            try:
                key = (i["name"], os.path.getsize(i["src"]))
            except OSError:
                key = None
            if key in primary_index:
                h = sha1_of(i["src"])
                if any(sha1_of(p) == h for p in primary_index[key]):
                    skipped_dupes.append(i["src"])
                    continue
        todo.append(i)
    stats["album_copies_skipped"] = len(skipped_dupes)

    claims = Counter(i["sidecar"] for i in todo if i["sidecar"])
    moved = already = conflicts = 0
    conflict_list = []
    for i in todo:
        target = os.path.join(dest, i["folder"], i["name"])
        if os.path.exists(target):
            if os.path.getsize(target) == os.path.getsize(i["src"]):
                already += 1
            else:
                conflicts += 1
                conflict_list.append(target)
            continue
        if args.apply:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(i["src"], target)
            if i["sidecar"]:
                # edited copies and numbered duplicates can share a sidecar: copy until the
                # last claim, then move it, so staging keeps only what was not used
                claims[i["sidecar"]] -= 1
                side_target = target + SUP + ".json"
                if claims[i["sidecar"]] > 0:
                    shutil.copyfile(i["sidecar"], side_target)
                else:
                    shutil.move(i["sidecar"], side_target)
        moved += 1
    if args.apply:                                      # folder descriptions, tiny
        for dirpath, _dirs, files in os.walk(gp):
            if "metadata.json" in files:
                folder = os.path.relpath(dirpath, gp)
                tdir = os.path.join(dest, "" if folder == "." else folder)
                if os.path.isdir(tdir) and not os.path.exists(os.path.join(tdir, "metadata.json")):
                    shutil.copyfile(os.path.join(dirpath, "metadata.json"),
                                    os.path.join(tdir, "metadata.json"))

    by_kind = Counter(os.path.splitext(i["name"])[1].lower() for i in items)
    lines = [
        f"Media files found in staging:      {stats['media_found']}",
        f"  with a matched sidecar:          {stats['with_sidecar']}",
        f"  without a sidecar:               {len(lists['no_sidecar'])}",
        f"Album copies identical to a year-folder photo (left in staging): {len(skipped_dupes)}",
        f"{'Moved' if args.apply else 'Would move'} into the archive:        {moved}",
        f"Already in the archive (same name and size): {already}",
        f"Name clashes with a different file (not touched): {conflicts}",
        f"JSON files that match no media:    {len(lists['orphan_json'])}",
        f"Other files left alone (not photo/video/json): {len(lists['other_files'])}",
        "Types: " + ", ".join(f"{k or '(none)'} {v}" for k, v in by_kind.most_common(12)),
    ]
    print("\n".join(lines))
    with open(args.report, "w", encoding="utf-8") as r:
        r.write("\n".join(lines) + "\n")
        for title, rows in (("MEDIA WITHOUT SIDECAR", lists["no_sidecar"]),
                            ("JSON MATCHING NO MEDIA", lists["orphan_json"]),
                            ("OTHER FILES LEFT ALONE", lists["other_files"]),
                            ("ALBUM COPIES SKIPPED", [os.path.relpath(p, gp) for p in skipped_dupes]),
                            ("NAME CLASHES", conflict_list)):
            r.write(f"\n== {title} ({len(rows)})\n")
            r.write("\n".join(rows) + ("\n" if rows else ""))
    print(f"Full lists: {args.report}")
    if not args.apply:
        print("Dry run only. Nothing was moved. Re-run with --apply to import.")


if __name__ == "__main__":
    main()
