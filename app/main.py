"""Photo Curator: read-only browse app over the scan index.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8090
Env:  ARCHIVE (originals folder), DB (index file), THUMBS (cache folder)

It never modifies, moves or deletes a photo: the index is opened read-only and
originals are only streamed back. Thumbnails are written to THUMBS.
"""
import io
import json
import mimetypes
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import posixpath

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import dupes as dupes_mod, similar, thumbs

HERE = Path(__file__).parent
ARCHIVE = os.path.realpath(os.environ.get(
    "ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
DB = os.environ.get("DB", os.path.join(os.path.dirname(ARCHIVE), "curator.sqlite"))
THUMBS = os.environ.get("THUMBS", os.path.join(os.path.dirname(ARCHIVE), "thumbs"))
# Your manual corrections live in their own file so a re-scan can never overwrite them.
EDITS = os.environ.get("EDITS", os.path.join(os.path.dirname(ARCHIVE), "curator_edits.sqlite"))
# Near-duplicate groups (derived data from scripts/find_similar.py; safe to delete and rebuild).
SIM = os.environ.get("SIM", os.path.join(os.path.dirname(ARCHIVE), "curator_similar.sqlite"))
# Pictures made FROM your photos (Best Take composites). Never inside originals/, never indexed as originals.
DERIVED = os.path.realpath(os.environ.get("DERIVED", os.path.join(os.path.dirname(ARCHIVE), "derived")))
PAGE_SIZE = 96

EDITS_SCHEMA = """
CREATE TABLE IF NOT EXISTS date_override (
  path TEXT PRIMARY KEY,          -- relative path, same as files.path
  taken_at TEXT NOT NULL,         -- ISO 8601 date you set
  prev_taken_at TEXT,             -- what the scan had, for the record
  prev_source TEXT,
  set_at TEXT NOT NULL
);
"""


def init_edits():
    con = sqlite3.connect(EDITS)
    con.executescript(EDITS_SCHEMA)
    con.executescript(dupes_mod.EDITS_SCHEMA)
    con.commit()
    con.close()


# `files` as the pages see it: the scan data with any manual date applied on top.
FILES = (
    "(SELECT f.id, f.path, f.kind, f.ext, f.size, f.mtime_ns, f.sha1, "
    "COALESCE(o.taken_at, f.taken_at) AS taken_at, "
    "CASE WHEN o.taken_at IS NOT NULL THEN 'manual' ELSE f.date_source END AS date_source, "
    "f.width, f.height, f.make, f.model, f.content_id, f.pair_key, f.has_sidecar "
    "FROM main.files f LEFT JOIN ed.date_override o ON o.path = f.path)"
)

mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/x-canon-cr2", ".cr2")
mimetypes.add_type("video/quicktime", ".mov")

app = FastAPI(title="Photo Curator")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def get_db():
    if not os.path.exists(DB):
        raise HTTPException(503, f"Index not found at {DB}. Run scripts/scan_archive.py first.")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    init_edits()
    similar.init_sim(SIM)
    con.execute("ATTACH DATABASE ? AS ed", (f"file:{EDITS}?mode=ro",))
    con.execute("ATTACH DATABASE ? AS sim", (f"file:{SIM}?mode=ro",))
    try:
        yield con
    finally:
        con.close()


def safe_abs(rel):
    """Resolve an indexed relative path, refusing anything outside the archive."""
    p = os.path.realpath(os.path.join(ARCHIVE, rel))
    if not p.startswith(ARCHIVE + os.sep):
        raise HTTPException(403, "Path outside archive")
    if not os.path.isfile(p):
        raise HTTPException(404, "File missing on disk")
    return p


def get_row(db, fid):
    row = db.execute(f"SELECT * FROM {FILES} files WHERE id = ?", (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "Unknown file")
    return row


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


templates.env.filters["human"] = human
def span_label(s):
    if s is None:
        return "camera times unknown"
    if s <= 10:
        return "taken within seconds of each other (burst or copies)"
    if s < 3600:
        return f"taken over {s // 60 or 1} min"
    if s < 86400:
        return f"taken over {s // 3600} h"
    return f"taken over {s // 86400} days (copies or repeat shots)"


templates.env.filters["span"] = span_label
templates.env.filters["day"] = lambda s: (s or "")[:10]

# A Live Photo's video is shown inside its still, not as a separate tile.
VISIBLE = "NOT (kind = 'video' AND pair_key IS NOT NULL)"


# The first folder under originals/ says where a photo came from. The grid's Source menu
# filters on it. Unknown folders are listed under their folder name.
SOURCE_LABELS = {
    "apple": "iCloud: main library",
    "apple-shared": "iCloud: shared library",
    "exports": "Old Photos library export",
    "photos-catchall": "Old photo folders",
    "google": "Google Photos",
    "scans": "Scanned prints",
}
SOURCE_EXPR = ("CASE WHEN instr(path, '/') > 0 THEN substr(path, 1, instr(path, '/') - 1) "
               "ELSE '' END")


def grid(request, db, source, year, kind, live, q, src, sort, page, month="", bbox=""):
    sources = [{"key": r["s"], "label": SOURCE_LABELS.get(r["s"], r["s"] or "(top level)"), "n": r["n"]}
               for r in db.execute(
                   f"SELECT {SOURCE_EXPR} AS s, COUNT(*) n FROM {FILES} files WHERE {VISIBLE} "
                   f"GROUP BY s ORDER BY n DESC")]
    where, args = [VISIBLE], []
    if source and any(x["key"] == source for x in sources):
        like = source.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("path LIKE ? ESCAPE '\\'")
        args.append(like + "/%")
    else:
        source = ""
    if year.isdigit():
        where.append("substr(taken_at,1,4) = ?")
        args.append(year)
    if month.isdigit() and 1 <= int(month) <= 12:
        where.append("substr(taken_at,6,2) = ?")
        args.append(f"{int(month):02d}")
    else:
        month = ""
    box = parse_bbox(bbox)
    if box:
        where.append("id IN (SELECT id FROM main.files WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)")
        args += [box[0], box[2], box[1], box[3]]
    else:
        bbox = ""
    if kind in ("photo", "video"):
        where.append("kind = ?")
        args.append(kind)
    if live == "1":
        where.append("kind = 'photo' AND pair_key IS NOT NULL")
    if q.strip():
        like = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("path LIKE ? ESCAPE '\\'")
        args.append(f"%{like}%")
    if src in ("exif", "google", "filename", "folder", "mtime", "manual"):
        where.append("date_source = ?")
        args.append(src)
    clause = " AND ".join(where)
    order = "taken_at ASC, id ASC" if sort == "old" else "taken_at DESC, id DESC"
    page = max(page, 1)

    total = db.execute(f"SELECT COUNT(*) FROM {FILES} files WHERE {clause}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT id, path, kind, ext, taken_at, pair_key, date_source FROM {FILES} files WHERE {clause} "
        f"ORDER BY {order} LIMIT ? OFFSET ?", args + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
    ).fetchall()
    years = db.execute(
        f"SELECT substr(taken_at,1,4) y, COUNT(*) n FROM {FILES} files WHERE {VISIBLE} "
        f"GROUP BY y ORDER BY y DESC").fetchall()
    grand = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM {FILES} files WHERE {VISIBLE}").fetchone()

    base = {"source": source, "year": year, "month": month, "kind": kind, "live": live, "q": q, "src": src, "sort": sort,
            "bbox": bbox}
    more_url = None
    if page * PAGE_SIZE < total:
        more_url = "/?" + urlencode({**{k: v for k, v in base.items() if v}, "page": page + 1})
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows, "total": total, "years": years, "grand": grand, "sources": sources,
        "f": base, "more_url": more_url, "page": page})


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db=Depends(get_db), source: str = "", year: str = "", kind: str = "",
          live: str = "", q: str = "", src: str = "", sort: str = "new", page: int = 1,
          month: str = "", bbox: str = ""):
    return grid(request, db, source, year, kind, live, q, src, sort, page, month, bbox)


def parse_bbox(text):
    """'south,west,north,east' -> (s, w, n, e) floats, or None."""
    try:
        s_, w, n, e = (float(x) for x in (text or "").split(","))
    except ValueError:
        return None
    if not (-90 <= s_ <= n <= 90 and -180 <= w <= e <= 180):
        return None
    return s_, w, n, e


# Background map. Leave as is for OpenStreetMap tiles (your browser fetches them from the internet; photos
# and their locations never leave the server). Set TILE_URL=none for a plain map with no outside requests,
# or point it at your own tile server.
TILE_URL = os.environ.get("TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
TILE_ATTRIBUTION = os.environ.get("TILE_ATTRIBUTION", "&copy; OpenStreetMap contributors")

GEO = ("(SELECT f.id, f.kind, f.pair_key, f.lat, f.lon, COALESCE(o.taken_at, f.taken_at) AS taken_at "
       "FROM main.files f LEFT JOIN ed.date_override o ON o.path = f.path WHERE f.lat IS NOT NULL)")


def geo_ready(db):
    return "lat" in {r[1] for r in db.execute("PRAGMA table_info(files)")}


def geo_filters(year, kind):
    where, args = ["NOT (kind = 'video' AND pair_key IS NOT NULL)"], []
    if year.isdigit():
        where.append("substr(taken_at,1,4) = ?")
        args.append(year)
    if kind in ("photo", "video"):
        where.append("kind = ?")
        args.append(kind)
    return " AND ".join(where), args


@app.get("/map", response_class=HTMLResponse)
def map_page(request: Request, db=Depends(get_db), year: str = "", kind: str = ""):
    ready = geo_ready(db)
    located = total = 0
    years = []
    if ready:
        located = db.execute(f"SELECT COUNT(*) FROM {GEO} g WHERE NOT (kind='video' AND pair_key IS NOT NULL)").fetchone()[0]
        years = db.execute(f"SELECT substr(taken_at,1,4) y, COUNT(*) n FROM {GEO} g "
                           "WHERE NOT (kind='video' AND pair_key IS NOT NULL) GROUP BY y ORDER BY y DESC").fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM {FILES} files WHERE {VISIBLE}").fetchone()[0]
    return templates.TemplateResponse(request, "map.html", {
        "ready": ready, "located": located, "total": total, "years": years,
        "f": {"year": year, "kind": kind}, "tile_url": "" if TILE_URL.lower() == "none" else TILE_URL,
        "tile_attr": TILE_ATTRIBUTION})


@app.get("/api/geo/summary")
def geo_summary(db=Depends(get_db), year: str = "", kind: str = ""):
    if not geo_ready(db):
        return {"n": 0}
    where, args = geo_filters(year, kind)
    r = db.execute(f"SELECT COUNT(*), MIN(lat), MIN(lon), MAX(lat), MAX(lon) FROM {GEO} g WHERE {where}", args).fetchone()
    return {"n": r[0], "bounds": [r[1], r[2], r[3], r[4]] if r[0] else None}


@app.get("/api/geo")
def geo_clusters(db=Depends(get_db), s: float = -90, w: float = -180, n: float = 90, e: float = 180,
                 zoom: int = 2, year: str = "", kind: str = ""):
    """Photos in view, grouped into grid cells that get finer as you zoom in."""
    if not geo_ready(db):
        return []
    s, n = max(-90.0, min(s, n)), min(90.0, max(s, n))
    w, e = max(-180.0, min(w, e)), min(180.0, max(w, e))
    cell = 84.375 / (2 ** max(0, min(zoom, 20)))
    where, args = geo_filters(year, kind)
    rows = db.execute(
        f"SELECT COUNT(*), AVG(lat), AVG(lon), MIN(lat), MIN(lon), MAX(lat), MAX(lon), MIN(id) FROM {GEO} g "
        f"WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND {where} "
        "GROUP BY CAST((lat + 90) / ? AS INTEGER), CAST((lon + 180) / ? AS INTEGER) LIMIT 2500",
        [s, n, w, e] + args + [cell, cell]).fetchall()
    return [{"n": r[0], "lat": r[1], "lon": r[2], "box": [r[3], r[4], r[5], r[6]], "id": r[7]} for r in rows]


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@app.get("/timeline", response_class=HTMLResponse)
def timeline(request: Request, db=Depends(get_db), source: str = "", dated: str = ""):
    """Year by month grid of how many items there are, so gaps in the collection stand out.

    By default every item counts. dated=1 leaves out dates that are only a guess (file date),
    which would otherwise hide a real gap behind an export-day pile-up.
    """
    where, args = [VISIBLE, "taken_at IS NOT NULL"], []
    if source:
        like = source.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("path LIKE ? ESCAPE '\\'")
        args.append(like + "/%")
    if dated == "1":
        where.append("date_source != 'mtime'")
    rows = db.execute(
        f"SELECT substr(taken_at,1,4) y, substr(taken_at,6,2) m, COUNT(*) n FROM {FILES} files "
        f"WHERE {' AND '.join(where)} GROUP BY y, m", args).fetchall()
    cells = {(r["y"], r["m"]): r["n"] for r in rows}
    years = sorted({y for y, _ in cells if y.isdigit() and 1990 <= int(y) <= datetime.now().year + 1})
    first, last = (years[0], years[-1]) if years else (None, None)
    table = []
    for y in range(int(first), int(last) + 1) if years else []:
        ys = str(y)
        months = [cells.get((ys, f"{m:02d}"), 0) for m in range(1, 13)]
        table.append({"year": ys, "months": months, "total": sum(months)})
    biggest = max([n for t in table for n in t["months"]] or [1])
    sources = [{"key": k, "label": v} for k, v in SOURCE_LABELS.items()]
    return templates.TemplateResponse(request, "timeline.html", {
        "table": table, "biggest": biggest, "month_names": MONTH_NAMES,
        "f": {"source": source, "dated": dated}, "sources": sources})


@app.get("/shared")
def shared():
    """Old bookmark: the shared library is now a Source filter on the main page."""
    return RedirectResponse("/?source=apple-shared", status_code=303)


@app.get("/photo/{fid}", response_class=HTMLResponse)
def detail(request: Request, fid: int, db=Depends(get_db)):
    row = get_row(db, fid)
    partner = None
    if row["pair_key"]:
        # The group can hold several copies of the same Live Photo (e.g. from different
        # exports), so look for a VIDEO, preferring one in the same folder as this still.
        partner = db.execute(
            f"SELECT * FROM {FILES} files WHERE pair_key = ? AND id != ? "
            f"ORDER BY (kind = 'video') DESC, (path LIKE ? ESCAPE '\\') DESC, id LIMIT 1",
            (row["pair_key"], fid,
             posixpath.dirname(row["path"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
        ).fetchone()
    live_video = None
    if row["kind"] == "photo" and partner and partner["kind"] == "video":
        live_video = partner
    twins = []
    if row["sha1"]:
        twins = db.execute("SELECT id, path FROM files WHERE sha1 = ? AND size = ? AND id != ?",
                           (row["sha1"], row["size"], fid)).fetchall()
    folder = posixpath.dirname(row["path"])
    n_guess = len(folder_targets(db, row, True))
    n_all = len(folder_targets(db, row, False))
    has_override = db.execute("SELECT 1 FROM ed.date_override WHERE path = ?",
                              (row["path"],)).fetchone() is not None
    similar_to = db.execute(
        f"SELECT f.id, f.path FROM sim.sim_group a JOIN sim.sim_group b ON a.group_id = b.group_id "
        f"JOIN {FILES} f ON f.path = b.path WHERE a.path = ? AND b.path != ?",
        (row["path"], row["path"])).fetchall()
    done = request.query_params.get("done", "")
    return templates.TemplateResponse(request, "detail.html", {
        "r": row, "live_video": live_video, "twins": twins, "similar_to": similar_to, "folder": folder,
        "n_guess": n_guess, "n_all": n_all, "has_override": has_override,
        "done": done if done.isdigit() else "", "err": request.query_params.get("err", "")[:80]})


def image_response(db, fid, kind):
    row = get_row(db, fid)
    ap = safe_abs(row["path"])
    out = thumbs.get_or_make(THUMBS, fid, ap, row["ext"], row["kind"] == "video", kind)
    if out:
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    label = row["ext"].lstrip(".").upper()
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="360" height="360">'
           '<rect width="100%" height="100%" fill="#2b2f36"/>'
           f'<text x="50%" y="50%" fill="#8b93a1" font-family="sans-serif" font-size="28" '
           f'text-anchor="middle" dominant-baseline="middle">{label}</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.get("/derived/{rel:path}")
def derived_file(rel: str):
    """View a picture made by scripts/best_take.py (read-only)."""
    p = os.path.realpath(os.path.join(DERIVED, rel))
    if not p.startswith(DERIVED + os.sep) or not os.path.isfile(p):
        raise HTTPException(404, "Not found")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/annotated/{fid}")
def annotated(fid: int, db=Depends(get_db)):
    """The preview with the faces the vision check found outlined: green eyes open, red closed, grey unclear."""
    from PIL import Image, ImageDraw
    row = get_row(db, fid)
    v = db.execute("SELECT detail FROM sim.vision WHERE path = ? AND status = 'ok'", (row["path"],)).fetchone()
    out = thumbs.get_or_make(THUMBS, fid, safe_abs(row["path"]), row["ext"], row["kind"] == "video", "preview")
    if not out:
        raise HTTPException(404, "No preview")
    im = Image.open(out).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    colours = {"open": (60, 200, 90), "closed": (229, 72, 77), "unclear": (170, 170, 170)}
    for f in (json.loads(v[0]) if v and v[0] else []):
        b = f["b"]
        d.rectangle([b[0] * w, b[1] * h, b[2] * w, b[3] * h], outline=colours.get(f["e"], (170, 170, 170)),
                    width=max(3, w // 300))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/thumb/{fid}")
def thumb(fid: int, db=Depends(get_db)):
    return image_response(db, fid, "thumb")


@app.get("/preview/{fid}")
def preview(fid: int, db=Depends(get_db)):
    return image_response(db, fid, "preview")


@app.get("/file/{fid}")
def original(fid: int, db=Depends(get_db)):
    """Stream the original, read-only."""
    row = get_row(db, fid)
    ap = safe_abs(row["path"])
    mt = mimetypes.guess_type(ap)[0] or "application/octet-stream"
    return FileResponse(ap, media_type=mt)


@app.get("/dups", response_class=HTMLResponse)
def dups(request: Request, db=Depends(get_db)):
    groups = db.execute(
        "SELECT sha1, size, COUNT(*) n FROM files WHERE sha1 IS NOT NULL "
        "GROUP BY sha1, size HAVING n > 1 ORDER BY size DESC LIMIT 200").fetchall()
    out = []
    for g in groups:
        members = db.execute(f"SELECT id, path, taken_at FROM {FILES} files WHERE sha1 = ? AND size = ? "
                             "ORDER BY path", (g["sha1"], g["size"])).fetchall()
        out.append({"size": g["size"], "members": members})
    return templates.TemplateResponse(request, "dups.html", {"groups": out})


# ---- exact-duplicate clean-up (the page only records approvals; scripts/quarantine.py moves files) ----

@app.get("/cleanup", response_class=HTMLResponse)
def cleanup(request: Request, db=Depends(get_db)):
    plan = dupes_mod.build_plan(db)
    approvals = {}
    for a in db.execute("SELECT * FROM ed.dup_approval ORDER BY id"):
        approvals.setdefault((a["keep_src"], a["remove_src"]), []).append(a)
    rows = []
    for (keep, rem), items in plan["pairs"].items():
        rows.append({"keep": keep, "remove": rem, "n": len(items),
                     "bytes": sum(i["victim"]["size"] for i in items),
                     "pending": [a for a in approvals.get((keep, rem), []) if not a["used_batch"]]})
    rows.sort(key=lambda r: -r["bytes"])
    held = [{"why": why, "n": len(items), "bytes": sum(i["victim"]["size"] for i in items)}
            for why, items in plan["held"].items()]
    batches = []
    for b in db.execute(
            "SELECT batch, COUNT(*) n, SUM(size) bytes, MAX(moved_at) last, "
            "SUM(restored_at IS NOT NULL) restored, SUM(purged_at IS NOT NULL) purged "
            "FROM ed.quarantine_log GROUP BY batch ORDER BY batch DESC"):
        d = dict(b)
        d["left"] = dupes_mod.retention_left(b["last"])
        batches.append(d)
    return templates.TemplateResponse(request, "cleanup.html", {
        "rows": rows, "held": held, "batches": batches, "groups": plan["groups"],
        "victims": plan["victims"], "total_bytes": sum(r["bytes"] for r in rows),
        "retention": dupes_mod.RETENTION_DAYS, "msg": request.query_params.get("msg", "")})


REVIEW_PAGE = 30


@app.get("/cleanup/review", response_class=HTMLResponse)
def cleanup_review(request: Request, keep: str, remove: str, db=Depends(get_db),
                   page: int = 1, order: str = "path"):
    plan = dupes_mod.build_plan(db)
    items = list(plan["pairs"].get((keep, remove), []))
    if order == "size":
        items.sort(key=lambda i: -i["victim"]["size"])
    elif order == "random":
        import random
        random.Random(int(request.query_params.get("seed", "1"))).shuffle(items)
    else:
        items.sort(key=lambda i: i["victim"]["path"])
    pages = max(1, -(-len(items) // REVIEW_PAGE))
    page = min(max(page, 1), pages)
    shown = items[(page - 1) * REVIEW_PAGE: page * REVIEW_PAGE]
    left_alone = [i for i in plan["held"].get(dupes_mod.EXCLUDED, [])
                  if dupes_mod.src_of(i["keeper"]["path"]) == keep and dupes_mod.src_of(i["victim"]["path"]) == remove]
    pending = db.execute("SELECT * FROM ed.dup_approval WHERE keep_src=? AND remove_src=? AND used_batch IS NULL",
                         (keep, remove)).fetchall()
    base = urlencode({"keep": keep, "remove": remove, "order": order, **(
        {"seed": request.query_params.get("seed", "1")} if order == "random" else {})})
    return templates.TemplateResponse(request, "cleanup_review.html", {
        "keep": keep, "remove": remove, "n": len(items), "shown": shown, "page": page, "pages": pages,
        "order": order, "base": base, "left_alone": left_alone, "pending": pending,
        "bytes": sum(i["victim"]["size"] for i in items), "first": (page - 1) * REVIEW_PAGE,
        "back": "/cleanup/review?" + base + f"&page={page}", "msg": request.query_params.get("msg", "")})


def safe_back(back):
    return back if back.startswith("/cleanup") and "//" not in back and "\n" not in back else "/cleanup"


@app.post("/cleanup/exclude")
async def cleanup_exclude(request: Request):
    if not same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    form = await read_form(request)
    path = form.get("path", "")
    con = sqlite3.connect(EDITS)
    try:
        if form.get("undo") == "1":
            con.execute("DELETE FROM dup_exclude WHERE path = ?", (path,))
        elif path:
            con.execute("INSERT OR REPLACE INTO dup_exclude(path, set_at) VALUES (?,?)",
                        (path, datetime.now().isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()
    return RedirectResponse(safe_back(form.get("back", "")), status_code=303)


@app.post("/cleanup/approve")
async def cleanup_approve(request: Request):
    if not same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    form = await read_form(request)
    keep, rem = form.get("keep", ""), form.get("remove", "")
    try:
        cap = int(form.get("cap", ""))
    except ValueError:
        cap = 0
    if not keep or not rem or not 1 <= cap <= 100000:
        return RedirectResponse(safe_back(form.get("back", "")) + ("&" if "?" in form.get("back", "") else "?")
                                + "msg=Enter+a+number+of+files+between+1+and+100000", status_code=303)
    con = sqlite3.connect(EDITS)
    try:
        con.execute("INSERT INTO dup_approval(keep_src, remove_src, cap, approved_at) VALUES (?,?,?,?)",
                    (keep, rem, cap, datetime.now().isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()
    back = safe_back(form.get("back", ""))
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg=Approved+up+to+{cap}+files.+Nothing+moves+until+you+run+scripts/quarantine.py+--apply", status_code=303)


@app.post("/cleanup/revoke")
async def cleanup_revoke(request: Request):
    if not same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    form = await read_form(request)
    con = sqlite3.connect(EDITS)
    try:
        con.execute("DELETE FROM dup_approval WHERE id = ? AND used_batch IS NULL", (form.get("id", ""),))
        con.commit()
    finally:
        con.close()
    back = safe_back(form.get("back", ""))
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg=Approval+withdrawn", status_code=303)


SIM_PAGE = 20


@app.get("/similar", response_class=HTMLResponse)
def similar_page(request: Request, db=Depends(get_db), page: int = 1, kind: str = "",
                 sort: str = "space", group: str = ""):
    page = max(page, 1)
    built = db.execute("SELECT v FROM sim.sim_meta WHERE k = 'built_at'").fetchone()
    thr = db.execute("SELECT v FROM sim.sim_meta WHERE k = 'threshold'").fetchone()
    tot = db.execute(
        f"SELECT COUNT(*) n, SUM(f.size) s, SUM(CASE WHEN g.is_best = 1 THEN f.size ELSE 0 END) b, "
        f"COUNT(DISTINCT g.group_id) gc FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path").fetchone()
    # Which groups to show. "cross" = the same picture in more than one source folder
    # (e.g. an iCloud original and an old export); "burst" = one source, shot within 10 s.
    src_g = ("CASE WHEN instr(g.path, '/') > 0 THEN substr(g.path, 1, instr(g.path, '/') - 1) "
             "ELSE '' END")
    where, args, having = [], [], []
    if group.isdigit():
        where.append("g.group_id = ?")
        args.append(int(group))
    if kind == "cross":
        having.append(f"COUNT(DISTINCT {src_g}) > 1")
    elif kind == "burst":
        where.append("i.span_s IS NOT NULL AND i.span_s <= 10")
        having.append(f"COUNT(DISTINCT {src_g}) = 1")
    elif kind == "spread":
        where.append("i.span_s > 10")
    elif kind == "unknown":
        where.append("i.span_s IS NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    hav = ("HAVING " + " AND ".join(having)) if having else ""
    order = "n DESC, spare DESC" if sort == "size" else "spare DESC, g.group_id"
    base = (f"FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path "
            f"LEFT JOIN sim.sim_info i ON i.group_id = g.group_id {clause} GROUP BY g.group_id {hav}")
    shown = db.execute(f"SELECT COUNT(*) FROM (SELECT g.group_id {base})", args).fetchone()[0]
    gids = db.execute(
        f"SELECT g.group_id, COUNT(*) n, SUM(f.size) - SUM(CASE WHEN g.is_best = 1 THEN f.size ELSE 0 END) AS spare "
        f"{base} ORDER BY {order} LIMIT ? OFFSET ?", args + [SIM_PAGE, (page - 1) * SIM_PAGE]).fetchall()
    groups = []
    for g in gids:
        rows = db.execute(
            f"SELECT f.id, f.path, f.taken_at, f.size, f.width, f.height, g.is_best, g.is_sharpest, h.sharp "
            f"FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path "
            f"LEFT JOIN sim.hashes h ON h.path = g.path WHERE g.group_id = ? "
            f"ORDER BY g.is_sharpest DESC, g.is_best DESC, f.size DESC", (g["group_id"],)).fetchall()
        members = []
        for r in rows:
            top = r["path"].split("/")[0] if "/" in r["path"] else ""
            members.append({**dict(r), "src_label": SOURCE_LABELS.get(top, top or "(top level)")})
        info = db.execute("SELECT span_s FROM sim.sim_info WHERE group_id = ?", (g["group_id"],)).fetchone()
        labels = sorted({m["src_label"] for m in members})
        groups.append({"id": g["group_id"], "spare": g["spare"], "members": members,
                       "span": info["span_s"] if info else None,
                       "sources": labels if len(labels) > 1 else []})
    f = {"kind": kind, "sort": sort, "group": group}
    q = {k: v for k, v in f.items() if v}
    more = page * SIM_PAGE < shown
    return templates.TemplateResponse(request, "similar.html", {
        "groups": groups, "tot": tot, "shown": shown, "f": f,
        "built": built[0] if built else None, "thr": thr[0] if thr else None, "page": page,
        "next_url": "/similar?" + urlencode({**q, "page": page + 1}) if more else None,
        "prev_url": "/similar?" + urlencode({**q, "page": page - 1}) if page > 1 else None})


# ---- near-duplicate Review: pick the one to keep, the rest go to quarantine ----
# The page only RECORDS picks (table sim_mark in the edits file).  scripts/quarantine.py --apply
# is what moves files, and it re-checks everything first.

REVIEW_PER = (5, 10, 25, 50)


def review_filters(kind, view):
    where, having = [], ["SUM(m.path IS NULL) > 0" if view != "decided" else "SUM(m.path IS NULL) = 0"]
    src_g = ("CASE WHEN instr(g.path, '/') > 0 THEN substr(g.path, 1, instr(g.path, '/') - 1) "
             "ELSE '' END")
    if kind == "cross":
        having.append(f"COUNT(DISTINCT {src_g}) > 1")
    elif kind == "burst":
        where.append("i.span_s IS NOT NULL AND i.span_s <= 10")
        having.append(f"COUNT(DISTINCT {src_g}) = 1")
    elif kind == "spread":
        where.append("i.span_s > 10")
    elif kind == "unknown":
        where.append("i.span_s IS NULL")
    return (("WHERE " + " AND ".join(where)) if where else ""), "HAVING " + " AND ".join(having)


def suggest_keeper(members):
    """Fewest closed eyes (if the vision check has run), then the largest picture; on a tie the one
    with a location, then a Live Photo, then the sharpest."""
    return max(members, key=lambda m: (-(m["eyes_closed"] or 0), (m["width"] or 0) * (m["height"] or 0), m["has_geo"],
                                       1 if m["pair_key"] else 0, m["sharp"] if m["sharp"] is not None else -1,
                                       m["size"]))["path"]


@app.get("/similar/review", response_class=HTMLResponse)
def similar_review(request: Request, db=Depends(get_db), kind: str = "", view: str = "pending",
                   order: str = "space", start: int = 0, per: int = 10, msg: str = ""):
    per = per if per in REVIEW_PER else 10
    start = max(start, 0)
    built = db.execute("SELECT v FROM sim.sim_meta WHERE k = 'built_at'").fetchone()
    where, having = review_filters(kind, view)
    base = (f"FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path "
            f"LEFT JOIN sim.sim_info i ON i.group_id = g.group_id "
            f"LEFT JOIN ed.sim_mark m ON m.path = g.path {where} GROUP BY g.group_id {having}")
    shown = db.execute(f"SELECT COUNT(*) FROM (SELECT g.group_id {base})").fetchone()[0]
    sort = ("g.group_id" if order == "group"
            else "SUM(f.size) - SUM(CASE WHEN g.is_best = 1 THEN f.size ELSE 0 END) DESC, g.group_id")
    gids = [r[0] for r in db.execute(f"SELECT g.group_id {base} ORDER BY {sort} LIMIT ? OFFSET ?", (per, start))]
    geo = "mf.lat IS NOT NULL" if geo_ready(db) else "0"
    groups = []
    for gid in gids:
        rows = db.execute(
            f"SELECT f.id, f.path, f.kind, f.size, f.width, f.height, f.taken_at, f.pair_key, {geo} AS has_geo, "
            f"g.is_best, g.is_sharpest, h.sharp, m.mark, m.keeper, "
            f"v.faces, v.eyes_closed, v.looking_away, v.note AS vnote "
            f"FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path JOIN main.files mf ON mf.id = f.id "
            f"LEFT JOIN sim.hashes h ON h.path = g.path LEFT JOIN ed.sim_mark m ON m.path = g.path "
            f"LEFT JOIN sim.vision v ON v.path = g.path AND v.status = 'ok' "
            f"WHERE g.group_id = ? ORDER BY f.path", (gid,)).fetchall()
        members = []
        for r in rows:
            top = r["path"].split("/")[0] if "/" in r["path"] else ""
            members.append({**dict(r), "src_label": SOURCE_LABELS.get(top, top or "(top level)")})
        sug = suggest_keeper(members)
        keeps = [m["path"] for m in members if m["mark"] == "keep"]
        if all(m["mark"] == "keep" for m in members):
            chosen = "all"
        elif len(keeps) == 1 and all(m["mark"] for m in members):
            chosen = keeps[0]
        else:
            chosen = sug
        info = db.execute("SELECT span_s FROM sim.sim_info WHERE group_id = ?", (gid,)).fetchone()
        labels = sorted({m["src_label"] for m in members})
        groups.append({"id": gid, "members": members, "suggested": sug, "chosen": chosen,
                       "decided": all(m["mark"] for m in members),
                       "span": info["span_s"] if info else None,
                       "sources": labels if len(labels) > 1 else []})
    pend = db.execute("SELECT COUNT(*), COALESCE(SUM(f.size), 0) FROM ed.sim_mark m JOIN main.files f "
                      "ON f.path = m.path WHERE m.mark = 'discard' AND m.used_batch IS NULL").fetchone()
    total = db.execute("SELECT COUNT(DISTINCT group_id) FROM sim.sim_group").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM (SELECT g.group_id FROM sim.sim_group g LEFT JOIN ed.sim_mark m "
                      "ON m.path = g.path GROUP BY g.group_id HAVING SUM(m.path IS NULL) = 0)").fetchone()[0]
    f = {"kind": kind, "view": view, "order": order, "per": per}
    q = urlencode({k: v for k, v in f.items() if v})
    nxt = start + len(gids)
    return templates.TemplateResponse(request, "similar_review.html", {
        "groups": groups, "f": f, "built": built[0] if built else None, "shown": shown, "start": start,
        "pend_n": pend[0], "pend_bytes": pend[1], "total": total, "done": done, "msg": msg,
        "back": "/similar/review?" + q,
        "next_url": f"/similar/review?{q}&start={nxt}" if nxt < shown else None,
        "prev_url": f"/similar/review?{q}&start={max(start - per, 0)}" if start > 0 else None})


@app.post("/similar/decide")
async def similar_decide(request: Request, db=Depends(get_db)):
    if not same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    qs = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    one = lambda k, d="": qs.get(k, [d])[0]
    view = one("view", "pending")
    start = int(one("start", "0")) if one("start", "0").isdigit() else 0
    now = datetime.now().isoformat(timespec="seconds")
    marks, clears = [], []
    stays = skipped = groups_saved = 0
    for gid in qs.get("g", []):
        paths = list(dict.fromkeys(qs.get(f"m{gid}", [])))
        pick = one(f"pick{gid}", "later")
        ok = 2 <= len(paths) <= 80
        if ok:
            ph = ",".join("?" * len(paths))
            in_group = db.execute(f"SELECT COUNT(DISTINCT group_id), COUNT(*) FROM sim.sim_group "
                                  f"WHERE path IN ({ph})", paths).fetchone()
            whole = db.execute("SELECT COUNT(*) FROM sim.sim_group WHERE group_id = "
                               "(SELECT group_id FROM sim.sim_group WHERE path = ?)", (paths[0],)).fetchone()[0]
            indexed = db.execute(f"SELECT COUNT(*) FROM main.files WHERE path IN ({ph})", paths).fetchone()[0]
            used = db.execute(f"SELECT COUNT(*) FROM ed.sim_mark WHERE used_batch IS NOT NULL "
                              f"AND path IN ({ph})", paths).fetchone()[0]
            ok = in_group[0] == 1 and in_group[1] == len(paths) and whole == len(paths) \
                and indexed == len(paths) and used == 0
        if not ok:                                   # page is out of date: leave this group alone
            skipped += 1
            stays += 1
            continue
        if pick == "later":
            clears.extend(paths)
            stays += view != "decided"
        elif pick == "all":
            marks.extend((p, "keep", None, now) for p in paths)
            groups_saved += 1
            stays += view == "decided"
        elif pick in paths:
            marks.extend((p, "keep" if p == pick else "discard", None if p == pick else pick, now) for p in paths)
            groups_saved += 1
            stays += view == "decided"
        else:
            skipped += 1
            stays += 1
    con = sqlite3.connect(EDITS)
    try:
        con.executemany("DELETE FROM sim_mark WHERE path = ? AND used_batch IS NULL", [(p,) for p in clears])
        con.executemany("INSERT OR REPLACE INTO sim_mark(path, mark, keeper, set_at) VALUES (?,?,?,?)", marks)
        con.commit()
    finally:
        con.close()
    back = one("back", "/similar/review")
    if not back.startswith("/similar/review") or "//" in back or "\n" in back:
        back = "/similar/review"
    discards = sum(1 for m in marks if m[1] == "discard")
    text = f"Saved {groups_saved} group{'' if groups_saved == 1 else 's'} ({discards} photo{'' if discards == 1 else 's'} marked to discard)."
    if skipped:
        text += f" {skipped} group{'' if skipped == 1 else 's'} changed since the page loaded and were left alone."
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}" + urlencode({"start": start + stays, "msg": text}), status_code=303)


# ---- manual date corrections (written to the separate edits file, never to photos) ----

def parse_taken(text):
    """'2019-07-14' or '2019-07-14T09:30' -> ISO string, or None if not a sensible date."""
    text = (text or "").strip()
    for fmt, fill in (("%Y-%m-%d", "T12:00:00"), ("%Y-%m-%dT%H:%M", ":00")):
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if 1900 <= d.year <= datetime.now().year + 1:
            return d.strftime("%Y-%m-%d") + (fill if fmt == "%Y-%m-%d" else d.strftime("T%H:%M") + fill)
    return None


def folder_targets(db, row, only_guessed):
    """Files directly inside this photo's folder (plus their Live partners) that a bulk
    date would change. only_guessed limits it to file-dated ones with no manual date."""
    folder = posixpath.dirname(row["path"])
    prefix = (folder + "/") if folder else ""
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = ("SELECT f.id, f.path, f.taken_at, f.date_source, f.pair_key FROM main.files f "
           "LEFT JOIN ed.date_override o ON o.path = f.path "
           "WHERE f.path LIKE ? ESCAPE '\\' AND instr(substr(f.path, ?), '/') = 0")
    args = [like, len(prefix) + 1]
    if only_guessed:
        sql += " AND f.date_source = 'mtime' AND o.path IS NULL"
    return partners(db, db.execute(sql, args).fetchall())


def partners(db, rows):
    """Add the other half of any Live Photo pair so still and video stay on one date."""
    out = {r["path"]: r for r in rows}
    keys = {r["pair_key"] for r in rows if r["pair_key"]}
    for k in keys:
        for r in db.execute("SELECT id, path, taken_at, date_source, pair_key FROM main.files "
                            "WHERE pair_key = ?", (k,)):
            out.setdefault(r["path"], r)
    return list(out.values())


def same_origin(request):
    """Cheap guard against another web page POSTing to this one from your browser."""
    o = request.headers.get("origin") or request.headers.get("referer")
    return not o or urlparse(o).netloc == request.headers.get("host")


async def read_form(request):
    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(body).items()}


def write_overrides(rows, taken):
    con = sqlite3.connect(EDITS)
    try:
        now = datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT OR REPLACE INTO date_override(path, taken_at, prev_taken_at, prev_source, set_at) "
            "VALUES (?,?,?,?,?)",
            [(r["path"], taken, r["taken_at"], r["date_source"], now) for r in rows])
        con.commit()
    finally:
        con.close()


def clear_overrides(rows):
    con = sqlite3.connect(EDITS)
    try:
        con.executemany("DELETE FROM date_override WHERE path = ?", [(r["path"],) for r in rows])
        con.commit()
    finally:
        con.close()


@app.post("/photo/{fid}/date")
async def set_date(request: Request, fid: int, db=Depends(get_db)):
    if not same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    form = await read_form(request)
    row = db.execute("SELECT id, path, taken_at, date_source, pair_key FROM main.files WHERE id = ?",
                     (fid,)).fetchone()
    if not row:
        raise HTTPException(404, "Unknown file")
    scope = form.get("scope", "one")
    if form.get("action") == "clear":
        targets = partners(db, [row])
        clear_overrides(targets)
        return RedirectResponse(f"/photo/{fid}?done={len(targets)}", status_code=303)
    taken = parse_taken(form.get("taken"))
    if not taken:
        return RedirectResponse(f"/photo/{fid}?err=Enter+a+date+like+2019-07-14", status_code=303)
    if scope == "folder":
        targets = folder_targets(db, row, form.get("only_guessed") == "1")
    else:
        targets = partners(db, [row])
    write_overrides(targets, taken)
    return RedirectResponse(f"/photo/{fid}?done={len(targets)}", status_code=303)
