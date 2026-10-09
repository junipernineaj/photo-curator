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

from PIL import Image, ImageStat

SIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
  path TEXT PRIMARY KEY,         -- relative path, same as files.path
  size INTEGER NOT NULL,         -- file size when hashed (to notice changes)
  mtime_ns INTEGER NOT NULL,
  dhash INTEGER,                 -- signed 64-bit; NULL when unusable
  status TEXT NOT NULL,          -- ok | flat | failed
  hashed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_group (
  group_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  is_best INTEGER NOT NULL DEFAULT 0,   -- suggestion only: the largest version
  PRIMARY KEY (group_id, path)
);
CREATE INDEX IF NOT EXISTS idx_sim_path ON sim_group(path);
CREATE TABLE IF NOT EXISTS sim_meta (k TEXT PRIMARY KEY, v TEXT);
"""


def init_sim(path):
    con = sqlite3.connect(path)
    con.executescript(SIM_SCHEMA)
    con.commit()
    con.close()


def dhash_image(jpeg_path):
    """Return (status, signed 64-bit hash or None) for a thumbnail file."""
    try:
        with Image.open(jpeg_path) as im:
            g = im.convert("L").resize((9, 8), Image.LANCZOS)
    except Exception:
        return "failed", None
    if ImageStat.Stat(g).stddev[0] < 4:        # blank/flat picture: every hash looks alike
        return "flat", None
    px = list(g.getdata())
    h = 0
    for row in range(8):
        for col in range(8):
            h = (h << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return "ok", h - (1 << 64) if h >= (1 << 63) else h


def _u(h):
    return h + (1 << 64) if h < 0 else h


def group_hashes(items, threshold=5, spread=3, max_bucket=400):
    """items: list of (key, signed_hash). Returns (groups, skipped_buckets).

    Candidate pairs come from 8 byte-sized slices of the hash: two hashes within 7 bits
    must agree exactly on at least one slice (pigeonhole), so we only compare within
    buckets. Pairs are merged closest-first, and two clusters only join if EVERY cross
    pair is within threshold+spread bits, which stops long "A looks like B looks like C"
    chains from swallowing unrelated photos.
    """
    if not 0 <= threshold <= 7:
        raise ValueError("threshold must be between 0 and 7")
    hs = [_u(h) for _, h in items]
    buckets = defaultdict(list)
    for i, h in enumerate(hs):
        for s in range(8):
            buckets[(s, (h >> (8 * s)) & 0xFF)].append(i)
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


def _worker(args):
    from . import thumbs
    fid, abs_path, ext, thumbs_dir = args
    out = thumbs.get_or_make(thumbs_dir, fid, abs_path, ext, False, "thumb")
    if not out:
        return fid, "failed", None
    status, h = dhash_image(out)
    return fid, status, h


def run(archive, db_path, thumbs_dir, sim_path, threshold=5, workers=2, limit=0, log=print):
    """Hash every photo that is new or changed, then rebuild the groups."""
    from multiprocessing import Pool
    init_sim(sim_path)
    main = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row
    # Photos only; a Live Photo's video is not compared.
    rows = main.execute("SELECT id, path, ext, size, mtime_ns, sha1, width, height FROM files "
                        "WHERE kind = 'photo' ORDER BY id").fetchall()
    sim = sqlite3.connect(sim_path)
    have = {r[0]: (r[1], r[2]) for r in sim.execute("SELECT path, size, mtime_ns FROM hashes")}
    todo = [r for r in rows if have.get(r["path"]) != (r["size"], r["mtime_ns"])]
    if limit:
        todo = todo[:limit]
    log(f"{len(rows)} photos in the index, {len(todo)} to fingerprint")
    by_id = {r["id"]: r for r in rows}
    jobs = [(r["id"], os.path.join(archive, r["path"]), r["ext"], thumbs_dir) for r in todo]
    now = datetime.now().isoformat(timespec="seconds")
    done = 0
    with Pool(max(1, workers)) as pool:
        for fid, status, h in pool.imap_unordered(_worker, jobs, chunksize=16):
            r = by_id[fid]
            sim.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?,?)",
                        (r["path"], r["size"], r["mtime_ns"], h, status, now))
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
    sim.execute("DELETE FROM sim_group")
    for gid, paths in enumerate(groups, 1):
        shas = {meta[p]["sha1"] for p in paths}
        if len(shas) == 1 and None not in shas:
            continue                      # byte-identical: already on the Duplicates page
        best = max(paths, key=lambda p: ((meta[p]["width"] or 0) * (meta[p]["height"] or 0),
                                         meta[p]["size"]))
        for p in paths:
            sim.execute("INSERT INTO sim_group VALUES (?,?,?)", (gid, p, 1 if p == best else 0))
        kept += 1
    sim.execute("INSERT OR REPLACE INTO sim_meta VALUES ('built_at', ?)", (now,))
    sim.execute("INSERT OR REPLACE INTO sim_meta VALUES ('threshold', ?)", (str(threshold),))
    sim.commit()
    stats = dict(sim.execute("SELECT status, COUNT(*) FROM hashes GROUP BY status").fetchall())
    sim.close()
    main.close()
    log(f"hashes: {stats}; similar groups: {kept}; crowded buckets skipped: {skipped}")
    return kept
