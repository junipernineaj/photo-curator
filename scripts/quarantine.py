#!/usr/bin/env python3
"""Move approved exact duplicates into a quarantine folder (never deletes, except --purge).

The web app only RECORDS what you approve on the Clean-up page.  This script is the only
thing that moves files, and it runs on the server, at your say-so.

    originals/google/2019/IMG_1.jpg  ->  originals/_quarantine/<batch>/google/2019/IMG_1.jpg

It is a rename on the same disk: instant, nothing copied, nothing overwritten.  The scanner
skips folders starting with "_", so quarantined files drop out of every page after the next
scan, but they stay on disk (and in the backup) until you purge them.

Before moving a file it re-reads BOTH the copy being moved and the copy being kept and checks
their SHA-1 again; any mismatch, missing file or surprise and that file is left alone.
At least one copy of every photo always stays.  Sidecars (.json, .xmp, .aae) travel with
their photo.  If only the moved copy had a date you set by hand, the date is copied to the keeper.

Usage:
  scripts/quarantine.py                    # DRY RUN: what the approvals on the Clean-up page would move
  scripts/quarantine.py --apply            # do it (one batch per run, named by date and time)
  scripts/quarantine.py --status           # batches, sizes, and days until each may be purged
  scripts/quarantine.py --restore BATCH [--apply]       # put a whole batch back
  scripts/quarantine.py --restore BATCH --path SUB      # only files whose path starts with SUB
  scripts/quarantine.py --purge BATCH [--apply]         # permanently delete; only after 30 days

After --apply or --restore --apply, re-run scripts/scan_archive.py then scripts/find_similar.py.
"""
import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import dupes  # noqa: E402

ARCHIVE_ROOT = "/media/aj9/Juniper13/photo-archive"
SIDECAR_EXT = (".json", ".xmp", ".aae")


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect(archive):
    edits = os.path.join(archive, "curator_edits.sqlite")
    dupes.ensure_schema(edits)
    db = sqlite3.connect(f"file:{os.path.join(archive, 'curator.sqlite')}?mode=ro", uri=True)
    db.execute("ATTACH DATABASE ? AS ed", (f"file:{edits}?mode=ro",))
    ed = sqlite3.connect(edits)
    ed.row_factory = sqlite3.Row
    return db, ed


def sidecars_for(abs_path):
    """Sidecar files that belong to this photo: IMG.jpg.json, IMG.jpg.supplemental-metadata.json,
    IMG.jpg.xmp, and IMG.xmp / IMG.aae (same stem)."""
    folder, name = os.path.split(abs_path)
    stem = os.path.splitext(name)[0]
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        low = n.lower()
        if n == name or not low.endswith(SIDECAR_EXT):
            continue
        if n.startswith(name + ".") or os.path.splitext(n)[0] == stem:
            out.append(os.path.join(folder, n))
    return out


def pending(archive, ed, db):
    """Approved, unused rules -> list of (approval row, [items]) using today's plan."""
    plan = dupes.build_plan(db)
    out, claimed = [], set()
    for a in ed.execute("SELECT * FROM dup_approval WHERE used_batch IS NULL ORDER BY id"):
        items = plan["pairs"].get((a["keep_src"], a["remove_src"]), [])
        items = [it for it in sorted(items, key=lambda it: it["victim"]["path"])
                 if it["victim"]["path"] not in claimed][: a["cap"]]
        claimed.update(it["victim"]["path"] for it in items)   # two approvals never move a file twice
        out.append((a, items))
    return out


def log_move(ed, batch, path, qpath, role, sha1, size, keeper, rule):
    ed.execute("INSERT INTO quarantine_log(batch,path,qpath,role,sha1,size,keeper,rule,moved_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)",
               (batch, path, qpath, role, sha1, size, keeper, rule,
                datetime.now().isoformat(timespec="seconds")))
    ed.commit()


def do_run(archive, apply):
    originals = os.path.join(archive, "originals")
    db, ed = connect(archive)
    work = pending(archive, ed, db)
    if not work:
        print("No approvals waiting.  Approve a rule on the Clean-up page first.")
        return
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"{'APPLYING' if apply else 'DRY RUN'} batch {batch}\n")
    total = moved = refused = 0
    manifest = None
    if apply:
        manifest = open(os.path.join(archive, f"quarantine-manifest-{batch}.tsv"), "a")
    keeper_hash = {}
    for a, items in work:
        rule = f"{a['keep_src']} -> {a['remove_src']}"
        print(f"Rule: keep {a['keep_src']}, quarantine {a['remove_src']}: "
              f"{len(items)} file(s) (approved cap {a['cap']})")
        total += len(items)
        if not apply:
            for it in items[:3]:
                print(f"   e.g. {it['victim']['path']}\n        (keeper {it['keeper']['path']})")
            continue
        n_ok = 0
        for it in items:
            v, k = it["victim"], it["keeper"]
            vabs, kabs = os.path.join(originals, v["path"]), os.path.join(originals, k["path"])
            why = None
            if v["path"] == k["path"]:
                why = "same file"
            elif not os.path.isfile(vabs) or not os.path.isfile(kabs):
                why = "file missing on disk"
            elif os.path.getsize(vabs) != v["size"] or os.path.getsize(kabs) != k["size"]:
                why = "size changed since scan"
            else:
                try:
                    if k["path"] not in keeper_hash:
                        keeper_hash[k["path"]] = sha1_of(kabs)
                    if keeper_hash[k["path"]] != v["sha1"] or sha1_of(vabs) != v["sha1"]:
                        why = "SHA-1 differs from the index"
                except OSError as e:
                    why = f"unreadable ({e})"
            if why:
                refused += 1
                print(f"   SKIP {v['path']}: {why}")
                continue
            if v["override"] and not k["override"]:
                ed.execute("INSERT OR IGNORE INTO date_override(path,taken_at,prev_taken_at,prev_source,set_at) "
                           "VALUES (?,?,NULL,'copied from quarantined duplicate',?)",
                           (k["path"], v["override"], datetime.now().isoformat(timespec="seconds")))
                ed.commit()
            files = [(vabs, v["path"], "photo")] + [
                (s, os.path.relpath(s, originals), "sidecar") for s in sidecars_for(vabs)]
            for sabs, srel, role in files:
                qrel = os.path.join(dupes.QUARANTINE, batch, srel)
                qabs = os.path.join(originals, qrel)
                if os.path.exists(qabs):
                    print(f"   SKIP {srel}: quarantine target exists")
                    continue
                os.makedirs(os.path.dirname(qabs), exist_ok=True)
                os.rename(sabs, qabs)
                log_move(ed, batch, srel, qrel, role, v["sha1"] if role == "photo" else None,
                         os.path.getsize(qabs), k["path"], rule)
                manifest.write(f"{srel}\t{qrel}\t{role}\t{k['path']}\n")
                manifest.flush()
            moved += 1
            n_ok += 1
        ed.execute("UPDATE dup_approval SET used_batch = ? WHERE id = ?", (batch, a["id"]))
        ed.commit()
        print(f"   moved {n_ok}")
    print()
    if apply:
        manifest.close()
        print(f"Done: {moved} of {total} moved, {refused} skipped.  Batch {batch} may be purged "
              f"after {dupes.RETENTION_DAYS} days.\nNow re-run: scripts/scan_archive.py then scripts/find_similar.py")
    else:
        print(f"Would move {total} file(s) (each is re-hashed against its keeper first).  "
              "Add --apply to do it.")


def rows_for(ed, batch, sub=None):
    q = "SELECT * FROM quarantine_log WHERE batch = ?"
    args = [batch]
    if sub:
        q += " AND path LIKE ? ESCAPE '\\'"
        args.append(sub.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return ed.execute(q, args).fetchall()


def do_restore(archive, batch, sub, apply):
    originals = os.path.join(archive, "originals")
    _, ed = connect(archive)
    rows = [r for r in rows_for(ed, batch, sub) if not r["restored_at"] and not r["purged_at"]]
    if not rows:
        print("Nothing to restore for that batch/path.")
        return
    print(f"{'RESTORING' if apply else 'DRY RUN: would restore'} {len(rows)} file(s) from batch {batch}")
    ok = skipped = 0
    for r in rows:
        q, o = os.path.join(originals, r["qpath"]), os.path.join(originals, r["path"])
        if not os.path.isfile(q):
            print(f"   SKIP {r['path']}: not in quarantine any more")
            skipped += 1
        elif os.path.exists(o):
            print(f"   SKIP {r['path']}: something is already at the original path")
            skipped += 1
        elif apply:
            os.makedirs(os.path.dirname(o), exist_ok=True)
            os.rename(q, o)
            ed.execute("UPDATE quarantine_log SET restored_at = ? WHERE id = ?",
                       (datetime.now().isoformat(timespec="seconds"), r["id"]))
            ed.commit()
            ok += 1
        else:
            ok += 1
    print(f"{ok} {'restored' if apply else 'restorable'}, {skipped} skipped.")
    if apply:
        print("Now re-run: scripts/scan_archive.py then scripts/find_similar.py")


def do_purge(archive, batch, apply):
    originals = os.path.join(archive, "originals")
    _, ed = connect(archive)
    rows = [r for r in rows_for(ed, batch) if not r["restored_at"] and not r["purged_at"]]
    if not rows:
        print("Nothing left to purge in that batch.")
        return
    newest = max(r["moved_at"] for r in rows)
    left = dupes.retention_left(newest)
    size = sum(r["size"] or 0 for r in rows)
    print(f"Batch {batch}: {len(rows)} file(s), {size / 1e9:.2f} GB, last moved {newest}")
    if left > 0:
        print(f"REFUSED: the {dupes.RETENTION_DAYS}-day window has {left:.1f} day(s) to run.")
        return
    if not apply:
        print("DRY RUN.  This would PERMANENTLY DELETE those files.  Add --apply to do it.")
        return
    if input(f"Type the batch name ({batch}) to permanently delete these files: ").strip() != batch:
        print("Not confirmed; nothing deleted.")
        return
    gone = 0
    for r in rows:
        q = os.path.join(originals, r["qpath"])
        if os.path.isfile(q):
            os.remove(q)
            gone += 1
        ed.execute("UPDATE quarantine_log SET purged_at = ? WHERE id = ?",
                   (datetime.now().isoformat(timespec="seconds"), r["id"]))
    ed.commit()
    for d, _, _ in os.walk(os.path.join(originals, dupes.QUARANTINE, batch), topdown=False):
        try:
            os.rmdir(d)
        except OSError:
            pass
    print(f"Deleted {gone} file(s).  The Juniper12 backup still has them until you prune it.")


def do_status(archive):
    _, ed = connect(archive)
    rows = ed.execute(
        "SELECT batch, COUNT(*) n, SUM(size) b, MAX(moved_at) last, "
        "SUM(restored_at IS NOT NULL) rest, SUM(purged_at IS NOT NULL) pur "
        "FROM quarantine_log GROUP BY batch ORDER BY batch").fetchall()
    if not rows:
        print("No quarantine batches yet.")
    for r in rows:
        left = dupes.retention_left(r["last"])
        state = "purgeable now" if left <= 0 else f"{left:.1f} days until purgeable"
        print(f"{r['batch']}: {r['n']} files, {(r['b'] or 0) / 1e9:.2f} GB, "
              f"{r['rest']} restored, {r['pur']} purged, {state}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", default=ARCHIVE_ROOT)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--restore", metavar="BATCH")
    ap.add_argument("--purge", metavar="BATCH")
    ap.add_argument("--path", help="with --restore: only files whose original path starts with this")
    a = ap.parse_args()
    if a.status:
        do_status(a.archive)
    elif a.restore:
        do_restore(a.archive, a.restore, a.path, a.apply)
    elif a.purge:
        do_purge(a.archive, a.purge, a.apply)
    else:
        do_run(a.archive, a.apply)


if __name__ == "__main__":
    main()
