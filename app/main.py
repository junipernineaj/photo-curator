"""Photo Curator: read-only browse app over the scan index.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8090
Env:  ARCHIVE (originals folder), DB (index file), THUMBS (cache folder)

It never modifies, moves or deletes a photo: the index is opened read-only and
originals are only streamed back. Thumbnails are written to THUMBS.
"""
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

from . import similar, thumbs

HERE = Path(__file__).parent
ARCHIVE = os.path.realpath(os.environ.get(
    "ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
DB = os.environ.get("DB", os.path.join(os.path.dirname(ARCHIVE), "curator.sqlite"))
THUMBS = os.environ.get("THUMBS", os.path.join(os.path.dirname(ARCHIVE), "thumbs"))
# Your manual corrections live in their own file so a re-scan can never overwrite them.
EDITS = os.environ.get("EDITS", os.path.join(os.path.dirname(ARCHIVE), "curator_edits.sqlite"))
# Near-duplicate groups (derived data from scripts/find_similar.py; safe to delete and rebuild).
SIM = os.environ.get("SIM", os.path.join(os.path.dirname(ARCHIVE), "curator_similar.sqlite"))
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
templates.env.filters["day"] = lambda s: (s or "")[:10]

# A Live Photo's video is shown inside its still, not as a separate tile.
VISIBLE = "NOT (kind = 'video' AND pair_key IS NOT NULL)"


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db=Depends(get_db), year: str = "", kind: str = "",
          live: str = "", q: str = "", src: str = "", sort: str = "new", page: int = 1):
    where, args = [VISIBLE], []
    if year.isdigit():
        where.append("substr(taken_at,1,4) = ?")
        args.append(year)
    if kind in ("photo", "video"):
        where.append("kind = ?")
        args.append(kind)
    if live == "1":
        where.append("kind = 'photo' AND pair_key IS NOT NULL")
    if q.strip():
        like = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("path LIKE ? ESCAPE '\\'")
        args.append(f"%{like}%")
    if src in ("exif", "folder", "mtime", "manual"):
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

    base = {"year": year, "kind": kind, "live": live, "q": q, "src": src, "sort": sort}
    more_url = None
    if page * PAGE_SIZE < total:
        more_url = "/?" + urlencode({**{k: v for k, v in base.items() if v}, "page": page + 1})
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows, "total": total, "years": years, "grand": grand,
        "f": base, "more_url": more_url, "page": page})


@app.get("/photo/{fid}", response_class=HTMLResponse)
def detail(request: Request, fid: int, db=Depends(get_db)):
    row = get_row(db, fid)
    partner = None
    if row["pair_key"]:
        partner = db.execute(f"SELECT * FROM {FILES} files WHERE pair_key = ? AND id != ?",
                             (row["pair_key"], fid)).fetchone()
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


SIM_PAGE = 20


@app.get("/similar", response_class=HTMLResponse)
def similar_page(request: Request, db=Depends(get_db), page: int = 1):
    page = max(page, 1)
    built = db.execute("SELECT v FROM sim.sim_meta WHERE k = 'built_at'").fetchone()
    thr = db.execute("SELECT v FROM sim.sim_meta WHERE k = 'threshold'").fetchone()
    tot = db.execute(
        f"SELECT COUNT(*) n, SUM(f.size) s, SUM(CASE WHEN g.is_best = 1 THEN f.size ELSE 0 END) b, "
        f"COUNT(DISTINCT g.group_id) gc FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path").fetchone()
    gids = db.execute(
        f"SELECT g.group_id, SUM(f.size) - SUM(CASE WHEN g.is_best = 1 THEN f.size ELSE 0 END) AS spare "
        f"FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path GROUP BY g.group_id "
        f"ORDER BY spare DESC, g.group_id LIMIT ? OFFSET ?", (SIM_PAGE, (page - 1) * SIM_PAGE)).fetchall()
    groups = []
    for g in gids:
        members = db.execute(
            f"SELECT f.id, f.path, f.taken_at, f.size, f.width, f.height, g.is_best "
            f"FROM sim.sim_group g JOIN {FILES} f ON f.path = g.path WHERE g.group_id = ? "
            f"ORDER BY g.is_best DESC, f.size DESC", (g["group_id"],)).fetchall()
        groups.append({"id": g["group_id"], "spare": g["spare"], "members": members})
    more = page * SIM_PAGE < (tot["gc"] or 0)
    return templates.TemplateResponse(request, "similar.html", {
        "groups": groups, "tot": tot, "built": built[0] if built else None,
        "thr": thr[0] if thr else None, "page": page,
        "next_url": f"/similar?page={page + 1}" if more else None,
        "prev_url": f"/similar?page={page - 1}" if page > 1 else None})


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
