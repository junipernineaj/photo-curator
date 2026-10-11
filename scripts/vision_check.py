#!/usr/bin/env python3
"""Ask a local vision model (Ollama, e.g. Qwen-VL) about the people in near-duplicate photos.

For each photo in a near-duplicate group it records how many faces are visible, how many people
have their eyes closed, how many are looking away, and a short note (motion blur, mid-sentence...).
The Review page shows this on each card and prefers the frame with the fewest closed eyes.
Advice only: it never changes a photo, and you still pick.

Everything stays on this machine: the model runs in your own Ollama and the photos are sent only
to it, on localhost.  Results go in curator_similar.sqlite (derived data; safe to delete).

  scripts/vision_check.py --list-models          # which models Ollama has
  scripts/vision_check.py --limit 20             # try 20 photos first and print what it says
  scripts/vision_check.py                        # all photos in groups you have not decided yet
  scripts/vision_check.py --all                  # include groups already decided
  scripts/vision_check.py --model qwen2.5vl:7b   # choose the model (default: first vision model found)

Resumable: Ctrl-C any time and run it again; finished photos are skipped.  Photos are handled in the
order the Review page shows them (most spare space first), so what you review first is done first.
Env: OLLAMA_URL (default http://127.0.0.1:11434), ARCHIVE, DB, THUMBS, SIM, EDITS.
"""
import argparse
import base64
import json
import os
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
except ImportError:
    if os.path.exists(VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
        os.execv(VENV_PY, [VENV_PY] + sys.argv)
    sys.exit("Pillow is missing. Run: python3 -m venv ~/venvs/curator && "
             "~/venvs/curator/bin/pip install -r requirements.txt")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import similar, thumbs  # noqa: E402

PROMPT = (
    "Look at the people in this photo and answer as JSON.\n"
    "faces: how many human faces are clearly visible.\n"
    "eyes_closed: how many of those people have their eyes closed or mid-blink.\n"
    "looking_away: how many of those people are not looking towards the camera.\n"
    "note: at most 12 words on anything that makes it a poor shot (motion blur, mouth open mid-word, "
    "someone cut off or hidden); empty if nothing.\n"
    "Use 0 when there are no people. Only count what you can clearly see; do not guess.")
SCHEMA = {"type": "object",
          "properties": {"faces": {"type": "integer"}, "eyes_closed": {"type": "integer"},
                         "looking_away": {"type": "integer"}, "note": {"type": "string"}},
          "required": ["faces", "eyes_closed", "looking_away", "note"]}


def ollama(url, path, body=None, timeout=180):
    req = urllib.request.Request(url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


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


def ask(url, model, jpeg_path):
    with open(jpeg_path, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    out = ollama(url, "/api/chat", {
        "model": model, "stream": False, "format": SCHEMA, "keep_alive": "30m",
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": PROMPT, "images": [img]}]})
    d = json.loads(out["message"]["content"])
    n = lambda k: max(0, int(d.get(k) or 0))
    faces = n("faces")
    return faces, min(n("eyes_closed"), faces), min(n("looking_away"), faces), str(d.get("note") or "")[:120]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=os.environ.get("ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
    ap.add_argument("--model")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--all", action="store_true", help="include groups you have already decided")
    ap.add_argument("--limit", type=int, default=0, help="look at at most N photos, and print each answer")
    ap.add_argument("--workers", type=int, default=1, help="requests at once (1 is fine; try 2 on a big GPU)")
    ap.add_argument("--retry", action="store_true", help="try earlier failures again")
    a = ap.parse_args()
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    if a.list_models:
        for m in ollama(url, "/api/tags", timeout=10).get("models", []):
            print(m["name"])
        return
    model = pick_model(url, a.model)

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
            if h and h[:3] == (size, mtime, model) and (h[3] == "ok" or not a.retry):
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
            return row, ask(url, model, jpg), None
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
                                "looking_away,note,seen_at) VALUES (?,?,?,?,'ok',?,?,?,?,?)",
                                (path, size, mtime, model, *res, now))
                    if a.limit:
                        print(f"  {path}: faces {res[0]}, eyes closed {res[1]}, looking away {res[2]}"
                              f"{' - ' + res[3] if res[3] else ''}")
                else:
                    bad += 1
                    con.execute("INSERT OR REPLACE INTO vision(path,size,mtime_ns,model,status,note,seen_at) "
                                "VALUES (?,?,?,?,'failed',?,?)", (path, size, mtime, model, err[:120], now))
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
