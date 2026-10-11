#!/usr/bin/env python3
"""Ask a local vision model (Ollama, e.g. Qwen-VL) about the people in near-duplicate photos.

For each photo in a near-duplicate group it works in two steps, because asking about a whole group
photo at once over-counts faces and misses blinks: (1) the model finds each face and gives a box,
(2) each face is cropped and the model says whether the eyes are open or closed.  It records
faces, eyes closed and the face boxes (so the Review page
can outline them: red = eyes closed).
The Review page shows this on each card and prefers the frame with the fewest closed eyes.
Advice only: it never changes a photo, and you still pick.

Everything stays on this machine: the model runs in your own Ollama and the photos are sent only
to it, on localhost.  Results go in curator_similar.sqlite (derived data; safe to delete).

  scripts/vision_check.py --list-models          # which models Ollama has
  scripts/vision_check.py --limit 20             # try 20 photos first and print what it says
  scripts/vision_check.py                        # all photos in groups you have not decided yet
  scripts/vision_check.py --match 2017_07_29     # only photos with this text in their path
  scripts/vision_check.py --all                  # include groups already decided
  scripts/vision_check.py --model qwen2.5vl:7b   # choose the model (default: first vision model found)

Resumable: Ctrl-C any time and run it again; finished photos are skipped.  Photos are handled in the
order the Review page shows them (most spare space first), so what you review first is done first.
Env: OLLAMA_URL (default http://127.0.0.1:11434), ARCHIVE, DB, THUMBS, SIM, EDITS.
"""
import argparse
import base64
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

VENV_PY = os.path.expanduser(os.environ.get("CURATOR_VENV", "~/venvs/curator") + "/bin/python")
try:
    import PIL  # noqa: F401
    import pillow_heif  # noqa: F401   (iPhone HEIC photos need it; without it previews silently fail)
except ImportError:
    if os.path.exists(VENV_PY) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.dirname(os.path.dirname(VENV_PY))):
        os.execv(VENV_PY, [VENV_PY] + sys.argv)
    sys.exit("Pillow and pillow-heif are needed. Run: python3 -m venv ~/venvs/curator && "
             "~/venvs/curator/bin/pip install -r requirements.txt")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image  # noqa: E402
from app import similar, thumbs  # noqa: E402

MODEL_TAG = "/faces-v2"        # stored with the model name; bump to redo everything after a method change
FIND_PROMPT = ("Locate every human face in the image, including small faces in the background. "
               "Output the bounding box of each face in JSON, like "
               "[{\"bbox_2d\": [x1, y1, x2, y2], \"label\": \"face\"}]. Output [] if there are no people.")
EYE_PROMPT = ("This is a close-up of one person's face. Look carefully at their eyes. "
              "Are the eyes open (you can see the eyeballs) or closed (lids shut, or mid-blink)? "
              "Answer with one word: OPEN, CLOSED or UNCLEAR (use UNCLEAR if the face is blurry, hidden, "
              "turned away or the eyes are covered).")
MAX_FACES = 12          # judge the biggest faces only
MIN_FACE_PX = 32        # smaller than this (in the 1600px preview) is too small to judge
SEND_LONG = 1400        # long side sent for face finding; a multiple of 28 so the model's coordinates match


def ollama(url, path, body=None, timeout=240):
    req = urllib.request.Request(url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise OSError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None


def pick_model(url, wanted):
    try:
        names = [m["name"] for m in ollama(url, "/api/tags", timeout=10).get("models", [])]
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Cannot reach Ollama at {url} ({e}). Is it running?  (ollama serve)")
    if wanted:
        if wanted not in names and not any(n.split(":")[0] == wanted for n in names):
            sys.exit(f"Ollama has no model called {wanted}. It has: {', '.join(names) or 'nothing'}")
        return wanted
    vis = [n for n in names if "vl" in n.lower() or "vision" in n.lower() or "llava" in n.lower()]
    if not vis:
        sys.exit("No vision model found in Ollama (looked for names containing vl/vision/llava). "
                 f"It has: {', '.join(names) or 'nothing'}.  Pull one, e.g.  ollama pull qwen2.5vl:7b")
    return vis[0]


def jpeg_bytes(im, quality=90):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def chat(url, model, prompt, images, schema=None, num_predict=700):
    """Raw text of the model's answer.  Capped in length: a model that starts to loop is cut off."""
    body = {"model": model, "stream": False, "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": num_predict},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(i).decode() for i in images]}]}
    if schema:
        body["format"] = schema
    return ollama(url, "/api/chat", body)["message"]["content"]


NUM = r"(-?\d+(?:\.\d+)?)"
BOX_RE = re.compile(r"\[\s*" + NUM + r"\s*,\s*" + NUM + r"\s*,\s*" + NUM + r"\s*,\s*" + NUM + r"\s*\]")


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0


def find_faces(url, model, im):
    """Boxes (x1,y1,x2,y2) as fractions of the image, biggest first."""
    w, h = im.size
    s = min(1.0, SEND_LONG / max(w, h))
    sw, sh = max(28, round(w * s / 28) * 28), max(28, round(h * s / 28) * 28)
    sent = im.resize((sw, sh), Image.LANCZOS)
    text = chat(url, model, FIND_PROMPT, [jpeg_bytes(sent)], None, num_predict=600)
    norm = "qwen3" in model.lower()                      # Qwen3-VL answers on a 0-1000 grid, Qwen2.5-VL in pixels
    boxes = []
    for m in BOX_RE.finditer(text):                      # works on complete or cut-off answers
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        fx = (lambda v: v / 1000) if norm else (lambda v: v / sw)
        fy = (lambda v: v / 1000) if norm else (lambda v: v / sh)
        b = [min(max(fx(min(x1, x2)), 0), 1), min(max(fy(min(y1, y2)), 0), 1),
             min(max(fx(max(x1, x2)), 0), 1), min(max(fy(max(y1, y2)), 0), 1)]
        if b[2] - b[0] < 0.005 or b[3] - b[1] < 0.005 or (b[2] - b[0]) * (b[3] - b[1]) > 0.5:
            continue                                      # nothing, or implausibly huge
        if any(iou(b, o) > 0.5 for o in boxes):
            continue                                      # the same face again
        boxes.append(b)
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    return boxes


RAW = False            # --raw: print the model's exact answers


def judge_faces(url, model, im, boxes):
    """One call per face crop -> list of (eyes, looking); 'unclear' for tiny faces or unreadable answers."""
    w, h = im.size
    out = []
    for b in boxes:
        bw, bh = (b[2] - b[0]) * w, (b[3] - b[1]) * h
        if min(bw, bh) < MIN_FACE_PX:
            out.append(("unclear", "camera"))
            continue
        px, py = bw * 0.15, bh * 0.15
        c = im.crop((max(0, int(b[0] * w - px)), max(0, int(b[1] * h - py)),
                     min(w, int(b[2] * w + px)), min(h, int(b[3] * h + py))))
        c.thumbnail((336, 336))
        text = chat(url, model, EYE_PROMPT, [jpeg_bytes(c)], None, num_predict=12)
        m = re.search(r"\b(open|closed|unclear)\b", text, re.I)
        out.append((m.group(1).lower() if m else "unclear", "camera"))
        if RAW:
            print(f"      face {len(out)} ({int(bw)}x{int(bh)}px): {text.strip()!r}")
    return out


def ask(url, model, jpeg_path):
    """-> (faces, eyes_closed, looking_away, note, detail_json)"""
    with Image.open(jpeg_path) as im0:
        im = im0.convert("RGB")
    boxes = find_faces(url, model, im)[:MAX_FACES]
    states = judge_faces(url, model, im, boxes) if boxes else []
    closed = sum(1 for e, _ in states if e == "closed")
    away = sum(1 for e, lk in states if lk == "away")
    unclear = sum(1 for e, _ in states if e == "unclear")
    detail = [{"b": [round(v, 4) for v in b], "e": e, "l": lk} for b, (e, lk) in zip(boxes, states)]
    note = f"{unclear} too small or unclear" if unclear else ""
    return len(boxes), closed, away, note, json.dumps(detail, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=os.environ.get("ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
    ap.add_argument("--model")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--all", action="store_true", help="include groups you have already decided")
    ap.add_argument("--limit", type=int, default=0, help="look at at most N photos, and print each answer")
    ap.add_argument("--match", help="only photos whose path contains this text (e.g. 2017_07_29)")
    ap.add_argument("--workers", type=int, default=1, help="requests at once (1 is fine; try 2 on a big GPU)")
    ap.add_argument("--retry", action="store_true", help="try earlier failures again")
    ap.add_argument("--raw", action="store_true", help="print the model's exact answer for each face")
    a = ap.parse_args()
    global RAW
    RAW = a.raw
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    if a.list_models:
        for m in ollama(url, "/api/tags", timeout=10).get("models", []):
            print(m["name"])
        return
    model = pick_model(url, a.model)
    tag = model + MODEL_TAG

    archive = os.path.realpath(a.archive)
    parent = os.path.dirname(archive)
    db = os.environ.get("DB", os.path.join(parent, "curator.sqlite"))
    simp = os.environ.get("SIM", os.path.join(parent, "curator_similar.sqlite"))
    edits = os.environ.get("EDITS", os.path.join(parent, "curator_edits.sqlite"))
    thumbs_dir = os.environ.get("THUMBS", os.path.join(parent, "thumbs"))
    similar.init_sim(simp)
    con = sqlite3.connect(simp)
    main_db = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    marked = set()
    if os.path.exists(edits):
        try:
            ed = sqlite3.connect(f"file:{edits}?mode=ro", uri=True)
            marked = {r[0] for r in ed.execute("SELECT path FROM sim_mark")}
        except sqlite3.Error:
            pass                                       # no picks recorded yet
    info = {r[1]: r for r in main_db.execute("SELECT id, path, ext, size, mtime_ns FROM files WHERE kind = 'photo'")}
    groups = {}
    for gid, path, best in con.execute("SELECT group_id, path, is_best FROM sim_group"):
        groups.setdefault(gid, []).append((path, best))
    order = []
    for gid, members in groups.items():
        paths = [p for p, _ in members if p in info]
        if len(paths) < 2 or (not a.all and all(p in marked for p in paths)):
            continue
        spare = sum(info[p][3] for p in paths) - max(info[p][3] for p in paths)
        order.append((-spare, gid, paths))
    order.sort()
    have = {r[0]: r[1:] for r in con.execute("SELECT path, size, mtime_ns, model, status FROM vision")}
    todo = []
    for _, _, paths in order:
        for p in paths:
            h = have.get(p)
            fid, _, ext, size, mtime = info[p]
            if h and h[:3] == (size, mtime, tag) and (h[3] == "ok" or not a.retry):
                continue
            if a.match and a.match.lower() not in p.lower():
                continue
            todo.append(info[p])
    if a.limit:
        todo = todo[:a.limit]
    print(f"Model {model}.  {len(todo)} photo(s) to look at in {len(order)} group(s).")
    if not todo:
        return

    def work(row):
        fid, path, ext, size, mtime = row
        jpg = thumbs.get_or_make(thumbs_dir, fid, os.path.join(archive, path), ext, False, "preview")
        if not jpg:
            return row, None, "could not make a preview"
        try:
            t = time.time()
            return row, ask(url, model, jpg) + (time.time() - t,), None
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            return row, None, f"{type(e).__name__}: {e}"

    t0, done, bad = time.time(), 0, 0
    try:
        with ThreadPoolExecutor(max(1, a.workers)) as pool:
            for row, res, err in pool.map(work, todo):
                fid, path, ext, size, mtime = row
                now = datetime.now().isoformat(timespec="seconds")
                if res:
                    con.execute("INSERT OR REPLACE INTO vision(path,size,mtime_ns,model,status,faces,eyes_closed,"
                                "looking_away,note,detail,seen_at) VALUES (?,?,?,?,'ok',?,?,?,?,?,?)",
                                (path, size, mtime, tag, *res[:5], now))
                    if a.limit:
                        print(f"  {path} (/annotated/{fid}): faces {res[0]}, eyes closed {res[1]}, "
                              f"looking away {res[2]}{' - ' + res[3] if res[3] else ''}  [{res[5]:.1f}s]")
                else:
                    bad += 1
                    con.execute("INSERT OR REPLACE INTO vision(path,size,mtime_ns,model,status,note,seen_at) "
                                "VALUES (?,?,?,?,'failed',?,?)", (path, size, mtime, tag, err[:120], now))
                    print(f"  FAILED {path}: {err}")
                done += 1
                if done % 5 == 0:
                    con.commit()
                if done % 50 == 0:
                    rate = done / (time.time() - t0)
                    print(f"  {done}/{len(todo)}  ({rate:.1f}/s, about {(len(todo) - done) / rate / 60:.0f} min left)")
    except KeyboardInterrupt:
        print("\nStopped; run it again to carry on.")
    con.commit()
    print(f"Done: {done} looked at, {bad} failed.")


if __name__ == "__main__":
    main()
