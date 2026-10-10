"""Exact-duplicate clean-up: which copy to keep, and the plan the web page and
scripts/quarantine.py both work from.  Nothing in here touches a photo.

A "group" is files with identical SHA-1 and size.  For each group one copy is the
keeper (never moved); every other copy is a victim.  Victims are summarised as rules,
"keep the copy in A, quarantine the copy in B", so thousands of groups can be approved
with a handful of decisions.
"""
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

RETENTION_DAYS = 30
QUARANTINE = "_quarantine"          # under originals/; the scanner skips folders starting with "_"

# Which source wins when bytes are identical.  First match wins; edit to taste.
# A source is the first folder, or the first two for the catch-all folders.
SOURCE_ORDER = [
    "google",                                       # Takeout copy: carries the .json sidecar
    "apple",                                        # iCloud originals
    "apple-shared",
    "exports",
    "photos-catchall/Aperture-Library-masters",
    "photos-catchall/iPhoto-Library-originals",
    "photos-catchall/Photos-Library-2014-masters",
    "photos-catchall/Photos-Library-small-originals",
    "photos-catchall/Pictures-juniper13",
    "photos-catchall/PicturesPreFreya",
    "photos-catchall/Images-Backup",
    "photos-catchall/Doha-masters",
    "photos-catchall/82EdgbastonRoad-masters",
    "photos-catchall/82EdgbastonRoad-aperture-masters",
    "photos-catchall/iPhoto-Library-mums-backup",
    "photos-catchall/Personal-Photos-aggregated",   # a collection of copies: lowest
]

EDITS_SCHEMA = """
CREATE TABLE IF NOT EXISTS date_override (      -- same table the app creates; here so scripts can rely on it
  path TEXT PRIMARY KEY,
  taken_at TEXT NOT NULL,
  prev_taken_at TEXT,
  prev_source TEXT,
  set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dup_approval (       -- one-shot approvals made on the Clean-up page
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  keep_src TEXT NOT NULL,
  remove_src TEXT NOT NULL,
  cap INTEGER NOT NULL,                         -- move at most this many files
  approved_at TEXT NOT NULL,
  used_batch TEXT                               -- set by quarantine.py when it has run
);
CREATE TABLE IF NOT EXISTS quarantine_log (     -- one row per file moved; the audit trail
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch TEXT NOT NULL,
  path TEXT NOT NULL,                           -- where it was (relative to originals/)
  qpath TEXT NOT NULL,                          -- where it is now (relative to originals/)
  role TEXT NOT NULL,                           -- photo | sidecar
  sha1 TEXT, size INTEGER,
  keeper TEXT,                                  -- the copy that stayed
  rule TEXT,                                    -- "keep_src -> remove_src"
  moved_at TEXT NOT NULL,
  restored_at TEXT,
  purged_at TEXT
);
CREATE TABLE IF NOT EXISTS dup_exclude (        -- copies you said to leave alone ("keep both")
  path TEXT PRIMARY KEY,
  set_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ql_batch ON quarantine_log(batch);
"""


EXCLUDED = "You chose to keep both copies"


def src_of(path):
    parts = path.split("/")
    if parts[0] in ("photos-catchall", "exports") and len(parts) > 2:
        return parts[0] + "/" + parts[1]
    return parts[0]


def src_rank(src):
    for i, s in enumerate(SOURCE_ORDER):
        if src == s:
            return i
    for i, s in enumerate(SOURCE_ORDER):
        if src.startswith(s + "/"):
            return i
    return len(SOURCE_ORDER)


def keeper_key(row):
    """Lower sorts first = preferred keeper: better source, has a sidecar, then the plainest
    (shortest) file name, then path order for a stable result."""
    p = row["path"]
    return (src_rank(src_of(p)), 0 if row["has_sidecar"] else 1, len(os.path.basename(p)), p)


def ensure_schema(path):
    con = sqlite3.connect(path)
    con.executescript(EDITS_SCHEMA)
    con.commit()
    con.close()


def build_plan(db):
    """db: sqlite3 connection on curator.sqlite with the edits file ATTACHed as `ed`.

    Returns {"pairs": {(keep_src, remove_src): [ {keeper:row, victim:row} ... ]},
             "held":  {reason: [ {keeper, victim} ... ]},
             "groups": n, "victims": n}"""
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT f.id, f.path, f.kind, f.size, f.sha1, f.pair_key, f.has_sidecar, "
        "       o.taken_at AS override "
        "FROM main.files f LEFT JOIN ed.date_override o ON o.path = f.path "
        "WHERE f.sha1 IS NOT NULL AND (f.sha1, f.size) IN "
        "  (SELECT sha1, size FROM main.files WHERE sha1 IS NOT NULL GROUP BY sha1, size HAVING COUNT(*) > 1)"
    ).fetchall()
    # Live Photo halves: a victim still/video is only moved if its partner half also has a
    # matching copy elsewhere, so a pair is never split into one kept half and one orphan.
    pair_shas = defaultdict(list)
    for r in db.execute("SELECT pair_key, path, sha1 FROM main.files WHERE pair_key IS NOT NULL"):
        pair_shas[r[0]].append((r[1], r[2]))

    excluded = {r[0] for r in db.execute("SELECT path FROM ed.dup_exclude")}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["sha1"], r["size"])].append(r)

    dup_shas = {k[0] for k in groups}
    pairs, held = defaultdict(list), defaultdict(list)
    victims = 0
    for members in groups.values():
        members.sort(key=keeper_key)
        keeper, rest = members[0], members[1:]
        for v in rest:
            victims += 1
            item = {"keeper": keeper, "victim": v}
            if v["path"] in excluded:
                held[EXCLUDED].append(item)
            elif v["override"] and keeper["override"] and v["override"] != keeper["override"]:
                held["Both copies have a different date you set by hand"].append(item)
            elif v["pair_key"] and any(
                    p != v["path"] and sh not in dup_shas for p, sh in pair_shas[v["pair_key"]]):
                held["Live Photo half whose partner has no matching copy"].append(item)
            else:
                pairs[(src_of(keeper["path"]), src_of(v["path"]))].append(item)
    return {"pairs": pairs, "held": held, "groups": len(groups), "victims": victims}


def retention_left(moved_at, now=None):
    """Days until a batch may be purged (0 or less = allowed)."""
    now = now or datetime.now()
    due = datetime.fromisoformat(moved_at) + timedelta(days=RETENTION_DAYS)
    return (due - now).total_seconds() / 86400
