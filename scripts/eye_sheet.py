#!/usr/bin/env python3
"""A contact sheet of faces the eye check called open / closed / unclear / turned away, so you can judge
the check by eye in a minute instead of opening photos one by one.

  scripts/eye_sheet.py --state closed            # 40 random faces called "closed"
  scripts/eye_sheet.py --state open --n 60
  scripts/eye_sheet.py --state away              # heads judged "turned away" (eyes not asked about)
  scripts/eye_sheet.py --state closed --seed 2   # a different random 40

It writes <archive>/../derived/eye-sheet.html and a few small pictures; open
http://192.168.1.40:8090/derived/eye-sheet.html .  Each cell shows the face, the strip of eyes the model was
shown, and a link to the whole photo with the faces outlined.  Read-only: it changes nothing but those files.
"""
import argparse
import html
import json
import os
import random
import sqlite3
import sys

VENV_PY = os.path.expanduser(os.environ.get("CURATOR_VENV", "~/venvs/curator") + "/bin/python")
try:
    import PIL  # noqa: F401
    import pillow_heif  # noqa: F401
except ImportError:
    if os.path.exists(VENV_PY) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.dirname(os.path.dirname(VENV_PY))):
        os.execv(VENV_PY, [VENV_PY] + sys.argv)
    sys.exit("Pillow and pillow-heif are needed (see requirements.txt).")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image  # noqa: E402
from app import facefind, thumbs  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=os.environ.get("ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
    ap.add_argument("--state", choices=("open", "closed", "unclear", "away"), default="closed")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    archive = os.path.realpath(a.archive)
    parent = os.path.dirname(archive)
    derived = os.environ.get("DERIVED", os.path.join(parent, "derived"))
    out = os.path.join(derived, "eye-sheet")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.endswith(".jpg"):
            os.remove(os.path.join(out, f))
    sim = sqlite3.connect(f"file:{os.environ.get('SIM', os.path.join(parent, 'curator_similar.sqlite'))}?mode=ro", uri=True)
    main_db = sqlite3.connect(f"file:{os.environ.get('DB', os.path.join(parent, 'curator.sqlite'))}?mode=ro", uri=True)
    info = {r[1]: r for r in main_db.execute("SELECT id, path, ext FROM files")}
    faces = []
    for path, detail in sim.execute("SELECT path, detail FROM vision WHERE status='ok' AND model LIKE '%/faces-v6' "
                                    "AND detail IS NOT NULL AND faces > 0"):
        for i, d in enumerate(json.loads(detail)):
            if "k" not in d:
                continue
            st = "away" if d.get("l") == "away" else d["e"]
            if st == a.state:
                faces.append((path, i, d))
    if not faces:
        sys.exit(f"No faces called '{a.state}' with the current method yet. Run scripts/vision_check.py first.")
    random.Random(a.seed).shuffle(faces)
    picks = faces[:a.n]
    thumbs_dir = os.environ.get("THUMBS", os.path.join(parent, "thumbs"))
    cells = []
    for n, (path, i, d) in enumerate(picks):
        fid, _, ext = info[path]
        jpg = thumbs.get_or_make(thumbs_dir, fid, os.path.join(archive, path), ext, False, "preview")
        if not jpg:
            continue
        with Image.open(jpg) as im0:
            im = im0.convert("RGB")
        w, h = im.size
        b = d["b"]
        px, py = (b[2] - b[0]) * w * 0.3, (b[3] - b[1]) * h * 0.3
        face = im.crop((max(0, int(b[0] * w - px)), max(0, int(b[1] * h - py)), min(w, int(b[2] * w + px)), min(h, int(b[3] * h + py))))
        face.thumbnail((240, 240))
        face.save(os.path.join(out, f"{n}f.jpg"), quality=88)
        g = facefind.geometry(d["k"], w, h)
        facefind.eye_band(im, d["k"], g, width=300).save(os.path.join(out, f"{n}e.jpg"), quality=88)
        cells.append(f'<div class="c"><img src="eye-sheet/{n}f.jpg"><br><img src="eye-sheet/{n}e.jpg"><br>'
                     f'<span>#{n + 1} turn {g["yaw"]:.2f}, eyes {int(g["d"])}px apart</span><br>'
                     f'<a href="/annotated/{fid}" target="_blank">whole photo</a> · {html.escape(os.path.basename(path))}</div>')
    page = ("<!doctype html><meta charset=utf-8><title>Eye check sheet</title><style>"
            "body{font:14px system-ui;margin:16px;background:#fafafa}.c{display:inline-block;vertical-align:top;width:250px;"
            "margin:6px;padding:8px;background:#fff;border:1px solid #ccc;border-radius:6px}.c img{max-width:100%}"
            ".c span{color:#555;font-size:12px}</style>"
            f"<h2>{len(cells)} faces the check called <b>{a.state}</b></h2>"
            "<p>Each cell: the face, then the strip of eyes the model saw. Count how many are really "
            f"{'closed' if a.state == 'closed' else a.state}.</p>" + "".join(cells))
    dest = os.path.join(derived, "eye-sheet.html")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"{len(cells)} faces (of {len(faces):,} called {a.state}). Open http://192.168.1.40:8090/derived/eye-sheet.html")


if __name__ == "__main__":
    main()
