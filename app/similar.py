"""Near-duplicate detection: a small perceptual fingerprint per photo, then grouping.

Everything here is READ-ONLY on the photos. Fingerprints are computed from the cached
360px thumbnails (made by app/thumbs.py), so HEIC and RAW previews are handled the same
way the browse page handles them. Results go in their own file (curator_similar.sqlite),
which is derived data and can be deleted and rebuilt at any time.

Fingerprint: "dHash" (difference hash), 64 bits. The image is shrunk to 9x8 greyscale and
each pixel is compared with its right-hand neighbour. Resized, re-saved or lightly
edited copies give hashes that differ in only a few bits. It does not cope with heavy
crops or rotations; those will not be grouped (a missed match is safe, a false one is
only a suggestion you review).
"""
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

from PIL import Image, ImageFilter, ImageStat

SIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
  path TEXT PRIMARY KEY,         -- relative path, same as files.path
  size INTEGER NOT NULL,         -- file size when hashed (to notice changes)
  mtime_ns INTEGER NOT NULL,
  dhash INTEGER,                 -- signed 64-bit; NULL when unusable
  sharp REAL,                    -- sharpness score of the thumbnail (higher = crisper)
  status TEXT NOT NULL,          -- ok | flat | failed
  hashed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_group (
  group_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  is_best INTEGER NOT NULL DEFAULT 0,   -- suggestion only: the largest version
  is_sharpest INTEGER NOT NULL DEFAULT 0,   -- suggestion only: the crispest version
  PRIMARY KEY (group_id, path)
);
CREATE TABLE IF NOT EXISTS sim_info (
  group_id INTEGER PRIMARY KEY,
  span_s INTEGER                 -- seconds between earliest and latest camera time; NULL if unknown
);
CREATE INDEX IF NOT EXISTS idx_sim_path ON sim_group(path);
CREATE TABLE IF NOT EXISTS sim_meta (k TEXT PRIMARY KEY, v TEXT);
"""


def init_sim(path):
    con = sqlite3.connect(path)
    con.executescript(SIM_SCHEMA)
    # Upgrade files made by the first version of this tool.
    for table, col, ddl in (("hashes", "sharp", "REAL"),
                            ("sim_group", "is_sharpest", "INTEGER NOT NULL DEFAULT 0")):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    con.commit()
    con.close()


_LAPLACE = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


def analyse_image(jpeg_path):
    """Return (status, signed 64-bit hash or None, sharpness or None) for a thumbnail."""
    try:
        with Image.open(jpeg_path) as im:
            full = im.convert("L")
            g = full.resize((9, 8), Image.LANCZOS)
            # Sharpness: spread of the Laplacian (edge response). Blurred shots score low.
            sharp = ImageStat.Stat(full.filter(_LAPLACE)).var[0]
    except Exception:
        return "failed", None, None
    if ImageStat.Stat(g).stddev[0] < 4:        # blank/flat picture: every hash looks alike
        return "flat", None, sharp
    px = list(g.getdata())
    h = 0
    for row in range(8):
        for col in range(8):
            h = (h << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return "ok", (h - (1 << 64) if h >= (1 << 63) else h), sharp


def _u(h):
    return h + (1 << 64) if h < 0 else h


def group_hashes(items, threshold=5, spread=3, max_bucket=2500):
    """items: list of (key, signed_hash). Returns (groups, skipped_buckets).

    Candidate pairs come from threshold+1 slices of the 64-bit hash: two hashes within
    `threshold` bits must agree exactly on at least one slice (pigeonhole), so we only compare
    within buckets. Fewer, wider slices than before (6 slices of 10 to 11 bits at the default,
    not 8 of one byte) keep the buckets small as the archive grows: with 90,000 photos a
    one-byte slice put about 350 in an average bucket and many above the limit. Pairs are
    merged closest-first, and two clusters only join if EVERY cross pair is within
    threshold+spread bits, which stops long "A looks like B looks like C" chains from
    swallowing unrelated photos.
    """
    if not 0 <= threshold <= 7:
        raise ValueError("threshold must be between 0 and 7")
    hs = [_u(h) for _, h in items]
    n_slices = threshold + 1
    bounds = [round(i * 64 / n_slices) for i in range(n_slices + 1)]
    buckets = defaultdict(list)
    for i, h in enumerate(hs):
        for s in range(n_slices):
            lo, hi = bounds[s], bounds[s + 1]
            buckets[(s, (h >> lo) & ((1 << (hi - lo)) - 1))].append(i)
    cands, skipped = {}, 0
    for members in buckets.values():
        if len(members) > max_bucket:
            skipped += 1
            continue
        for a in range(len(members)):
            ia = members[a]
            for b in range(a + 1, len(members)):
                ib = members[b]
                d = (hs[ia] ^ hs[ib]).bit_count()
                if d <= threshold:
                    cands[(ia, ib) if ia < ib else (ib, ia)] = d
    cluster = {}                      # item index -> cluster id
    members_of = {}                   # cluster id -> list of item indexes
    next_id = 0
    for (a, b), _d in sorted(cands.items(), key=lambda kv: kv[1]):
        ca, cb = cluster.get(a), cluster.get(b)
        if ca is not None and ca == cb:
            continue
        ma = members_of[ca] if ca is not None else [a]
        mb = members_of[cb] if cb is not None else [b]
        if any((hs[x] ^ hs[y]).bit_count() > threshold + spread for x in ma for y in mb):
            continue
        if ca is None and cb is None:
            ca = next_id
            next_id += 1
            members_of[ca] = [a, b]
        elif ca is None:
            ca = cb
            members_of[ca].append(a)
        elif cb is None:
            members_of[ca].append(b)
        else:
            members_of[ca].extend(members_of.pop(cb))
        for x in members_of[ca]:
            cluster[x] = ca
    groups = [[items[i][0] for i in sorted(m)] for m in members_of.values() if len(m) > 1]
    return groups, skipped


def _span_seconds(meta, paths):
    """Seconds between the earliest and latest CAMERA time in the group, else None."""
    times = []
    for p in paths:
        if meta[p]["date_source"] == "exif" and meta[p]["taken_at"]:
            try:
                times.append(datetime.fromisoformat(meta[p]["taken_at"]))
            except ValueError:
                pass
    if len(times) < 2:
        return None
    return int((max(times) - min(times)).total_seconds())


def _worker(args):
    from . import thumbs
    fid, abs_path, ext, thumbs_dir = args
    out = thumbs.get_or_make(thumbs_dir, fid, abs_path, ext, False, "thumb")
    if not out:
        return fid, "failed", None, None
    status, h, sharp = analyse_image(out)
    return fid, status, h, sharp


def run(archive, db_path, thumbs_dir, sim_path, threshold=5, workers=2, limit=0, log=print):
    """Hash every photo that is new or changed, then rebuild the groups."""
    from multiprocessing import Pool
    init_sim(sim_path)
    main = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row
    # Photos only; a Live Photo's video is not compared.
    rows = main.execute("SELECT id, path, ext, size, mtime_ns, sha1, width, height, taken_at, date_source "
                        "FROM files "
                        "WHERE kind = 'photo' ORDER BY id").fetchall()
    sim = sqlite3.connect(sim_path)
    have = {r[0]: (r[1], r[2]) for r in sim.execute("SELECT path, size, mtime_ns FROM hashes")}
    # Also redo photos fingerprinted before sharpness existed (cheap: thumbnails are cached).
    no_sharp = {r[0] for r in sim.execute(
        "SELECT path FROM hashes WHERE status = 'ok' AND sharp IS NULL")}
    # Retry earlier failures too: the cause may have been fixed (the thumbnail step leaves a
    # ".fail" marker, so a file that still cannot be read is skipped quickly).
    failed = {r[0] for r in sim.execute("SELECT path FROM hashes WHERE status = 'failed'")}
    todo = [r for r in rows
            if have.get(r["path"]) != (r["size"], r["mtime_ns"]) or r["path"] in no_sharp
            or r["path"] in failed]
    if limit:
        todo = todo[:limit]
    log(f"{len(rows)} photos in the index, {len(todo)} to fingerprint")
    by_id = {r["id"]: r for r in rows}
    jobs = [(r["id"], os.path.join(archive, r["path"]), r["ext"], thumbs_dir) for r in todo]
    now = datetime.now().isoformat(timespec="seconds")
    done = 0
    with Pool(max(1, workers)) as pool:
        for fid, status, h, sharp in pool.imap_unordered(_worker, jobs, chunksize=16):
            r = by_id[fid]
            sim.execute("INSERT OR REPLACE INTO hashes(path, size, mtime_ns, dhash, sharp, status, hashed_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (r["path"], r["size"], r["mtime_ns"], h, sharp, status, now))
            done += 1
            if done % 500 == 0:
                sim.commit()
                log(f"  fingerprinted {done}/{len(jobs)}")
    sim.commit()

    # Forget files that left the index.
    live = {r["path"] for r in rows}
    for (p,) in sim.execute("SELECT path FROM hashes").fetchall():
        if p not in live:
            sim.execute("DELETE FROM hashes WHERE path = ?", (p,))
    items = [(p, h) for p, h in sim.execute("SELECT path, dhash FROM hashes WHERE status = 'ok'")]
    groups, skipped = group_hashes(items, threshold=threshold)
    meta = {r["path"]: r for r in rows}
    kept = 0
    sharp_of = {p: s_ for p, s_ in sim.execute("SELECT path, sharp FROM hashes")}
    sim.execute("DELETE FROM sim_group")
    sim.execute("DELETE FROM sim_info")
    for gid, paths in enumerate(groups, 1):
        shas = {meta[p]["sha1"] for p in paths}
        if len(shas) == 1 and None not in shas:
            continue                      # byte-identical: already on the Duplicates page
        best = max(paths, key=lambda p: ((meta[p]["width"] or 0) * (meta[p]["height"] or 0),
                                         meta[p]["size"]))
        sharpest = max(paths, key=lambda p: (sharp_of.get(p) or -1,
                                             (meta[p]["width"] or 0) * (meta[p]["height"] or 0)))
        for p in paths:
            sim.execute("INSERT INTO sim_group(group_id, path, is_best, is_sharpest) VALUES (?,?,?,?)",
                        (gid, p, 1 if p == best else 0, 1 if p == sharpest else 0))
        sim.execute("INSERT INTO sim_info VALUES (?,?)", (gid, _span_seconds(meta, paths)))
        kept += 1
    sim.execute("INSERT OR REPLACE INTO sim_meta VALUES ('built_at', ?)", (now,))
    sim.execute("INSERT OR REPLACE INTO sim_meta VALUES ('threshold', ?)", (str(threshold),))
    sim.commit()
    stats = dict(sim.execute("SELECT status, COUNT(*) FROM hashes GROUP BY status").fetchall())
    sim.close()
    main.close()
    log(f"hashes: {stats}; similar groups: {kept}; crowded buckets skipped: {skipped}")
    return kept
