"""Photo Curator: read-only browse app over the scan index.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8090
Env:  ARCHIVE (originals folder), DB (index file), THUMBS (cache folder)

It never modifies, moves or deletes a photo: the index is opened read-only and
originals are only streamed back. Thumbnails are written to THUMBS.
"""
import mimetypes
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import thumbs

HERE = Path(__file__).parent
ARCHIVE = os.path.realpath(os.environ.get(
    "ARCHIVE", "/media/aj9/Juniper13/photo-archive/originals"))
DB = os.environ.get("DB", os.path.join(os.path.dirname(ARCHIVE), "curator.sqlite"))
THUMBS = os.environ.get("THUMBS", os.path.join(os.path.dirname(ARCHIVE), "thumbs"))
PAGE_SIZE = 96

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
    row = db.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
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
    if src in ("exif", "folder", "mtime"):
        where.append("date_source = ?")
        args.append(src)
    clause = " AND ".join(where)
    order = "taken_at ASC, id ASC" if sort == "old" else "taken_at DESC, id DESC"
    page = max(page, 1)

    total = db.execute(f"SELECT COUNT(*) FROM files WHERE {clause}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT id, path, kind, ext, taken_at, pair_key, date_source FROM files WHERE {clause} "
        f"ORDER BY {order} LIMIT ? OFFSET ?", args + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
    ).fetchall()
    years = db.execute(
        f"SELECT substr(taken_at,1,4) y, COUNT(*) n FROM files WHERE {VISIBLE} "
        f"GROUP BY y ORDER BY y DESC").fetchall()
    grand = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE {VISIBLE}").fetchone()

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
        partner = db.execute("SELECT * FROM files WHERE pair_key = ? AND id != ?",
                             (row["pair_key"], fid)).fetchone()
    live_video = None
    if row["kind"] == "photo" and partner and partner["kind"] == "video":
        live_video = partner
    twins = []
    if row["sha1"]:
        twins = db.execute("SELECT id, path FROM files WHERE sha1 = ? AND size = ? AND id != ?",
                           (row["sha1"], row["size"], fid)).fetchall()
    return templates.TemplateResponse(request, "detail.html", {
        "r": row, "live_video": live_video, "twins": twins})


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
        members = db.execute("SELECT id, path, taken_at FROM files WHERE sha1 = ? AND size = ? "
                             "ORDER BY path", (g["sha1"], g["size"])).fetchall()
        out.append({"size": g["size"], "members": members})
    return templates.TemplateResponse(request, "dups.html", {"groups": out})
