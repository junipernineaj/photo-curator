#!/usr/bin/env python3
"""Best Take: build one picture from a burst where everyone's eyes are open.

Uses what scripts/vision_check.py found (a box round every head, and whether the eyes are open).
The frame with the fewest closed eyes is the base; every head with closed eyes is replaced by the same
person's head from another frame where their eyes are open.  Real pixels only; nothing is invented.
The originals are never touched.

Normally you do this on the Review page (the "Best Take" link on a group), see the before and after,
and approve it there.  This script does the other half:

  scripts/best_take.py --list               # groups with eye data and at least one closed eye
  scripts/best_take.py --group 4321         # make one (files go in <archive>/derived/best-take/)
  scripts/best_take.py --apply              # add the results you approved on the Review page to the
                                            # archive (originals/best-take/), with a provenance file,
                                            # then scan so they show up; for "add and discard the
                                            # other frames" it records those picks too
After --apply with discards, run scripts/quarantine.py (dry run first), then scripts/scan_archive.py.

Needs OpenCV:  ~/venvs/curator/bin/pip install opencv-python-headless
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

VENV_PY = os.path.expanduser(os.environ.get("CURATOR_VENV", "~/venvs/curator") + "/bin/python")
try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
    import pillow_heif  # noqa: F401
except ImportError:
    if os.path.exists(VENV_PY) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.dirname(os.path.dirname(VENV_PY))):
        os.execv(VENV_PY, [VENV_PY] + sys.argv)
    sys.exit("Needs OpenCV, Pillow and pillow-heif. Run: ~/venvs/curator/bin/pip install opencv-python-headless pillow-heif")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import besttake, dupes  # noqa: E402

def apply_approved(archive, parent):
    """Copy approved Best Takes into originals/best-take/ (never overwriting) and scan."""
    edits = os.environ.get("EDITS", os.path.join(parent, "curator_edits.sqlite"))
    dupes.ensure_schema(edits)
    derived = os.environ.get("DERIVED", os.path.join(parent, "derived"))
    ed = sqlite3.connect(edits)
    ed.row_factory = sqlite3.Row
    rows = ed.execute("SELECT * FROM besttake_approval WHERE used_at IS NULL ORDER BY id").fetchall()
    if not rows:
        print("No approved Best Takes waiting.")
        return
    dest_dir = os.path.join(archive, "best-take")
    os.makedirs(dest_dir, exist_ok=True)
    now = lambda: datetime.now().isoformat(timespec="seconds")
    done = 0
    for r in rows:
        src = os.path.join(derived, r["out_rel"])
        if not os.path.isfile(src):
            print(f"SKIP group {r['gid']}: {r['out_rel']} is not there any more")
            continue
        stem, ext = os.path.splitext(os.path.basename(src))
        name, n = stem + ext, 1
        while os.path.exists(os.path.join(dest_dir, name)):
            n += 1
            name = f"{stem}-{n}{ext}"
        dest = os.path.join(dest_dir, name)
        with open(src, "rb") as f, open(dest, "xb") as g:          # "x": never overwrite
            shutil.copyfileobj(f, g)
        if os.path.getsize(dest) != os.path.getsize(src):
            print(f"SKIP group {r['gid']}: copy size mismatch, left {dest} for you to look at")
            continue
        made_from = json.loads(r["made_from"])
        with open(dest + ".provenance.json", "w") as f:
            json.dump({"what": "Best Take: assembled from several frames of a burst; not an original",
                       "base": r["base"], "swaps": json.loads(r["swaps"]), "made_from": made_from,
                       "group": r["gid"], "approved_at": r["approved_at"], "added_at": now()}, f, indent=1)
        rel = "best-take/" + name
        ed.execute("UPDATE besttake_approval SET used_at = ?, archive_path = ? WHERE id = ?", (now(), rel, r["id"]))
        if r["mode"] == "add_discard":
            ed.execute("INSERT OR REPLACE INTO sim_mark(path, mark, keeper, set_at) VALUES (?, 'keep', NULL, ?)", (rel, now()))
            for p in made_from:
                ed.execute("INSERT OR REPLACE INTO sim_mark(path, mark, keeper, set_at) "
                           "SELECT ?, 'discard', ?, ? WHERE NOT EXISTS "
                           "(SELECT 1 FROM sim_mark WHERE path = ? AND used_batch IS NOT NULL)", (p, rel, now(), p))
        ed.commit()
        done += 1
        print(f"Added {rel}" + ("  (the other frames are marked discard)" if r["mode"] == "add_discard" else ""))
    print(f"{done} added.  Scanning so they appear...")
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_archive.py")],
                   env={**os.environ, "ARCHIVE": archive}, check=False)
    if any(r["mode"] == "add_discard" for r in rows):
        print("\nNow run scripts/quarantine.py (dry run first) to move the frames you chose to discard.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=os.environ.get("ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
    ap.add_argument("--group", type=int)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", action="store_true", help="add approved Best Takes to the archive")
    a = ap.parse_args()
    archive = os.path.realpath(a.archive)
    parent = os.path.dirname(archive)
    sim = sqlite3.connect(f"file:{os.environ.get('SIM', os.path.join(parent, 'curator_similar.sqlite'))}?mode=ro", uri=True)
    out_dir = os.environ.get("DERIVED", os.path.join(parent, "derived")) + "/best-take"
    if a.apply:
        apply_approved(archive, parent)
        return
    if a.list:
        rows = sim.execute(
            "SELECT g.group_id, COUNT(*), SUM(json_array_length(v.detail)), "
            "SUM((SELECT COUNT(*) FROM json_each(v.detail) j WHERE json_extract(j.value,'$.e')='closed')), MIN(g.path) "
            "FROM sim_group g JOIN vision v ON v.path = g.path AND v.status='ok' AND v.detail IS NOT NULL "
            "GROUP BY g.group_id HAVING SUM(json_array_length(v.detail)) > 0 ORDER BY g.group_id").fetchall()
        for gid, n, faces, closed, p in rows:
            if closed:
                print(f"group {gid}: {n} frames, {faces} faces seen, {closed} closed in total  ({p})")
        return
    if not a.group:
        sys.exit("Give --group N (or --list)")
    members = besttake.group_members(sim, a.group)
    if len(members) < 2:
        sys.exit("That group has fewer than 2 photos with eye data. Run scripts/vision_check.py first.")
    res = besttake.make(archive, out_dir, members)
    if res:
        print(f"Compare picture: {res['compare']}")


if __name__ == "__main__":
    main()
