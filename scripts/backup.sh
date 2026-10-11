#!/usr/bin/env bash
# One-command backup of the photo archive to the second disk.
#
#   scripts/backup.sh                # DRY RUN: shows what would be copied, changes nothing
#   scripts/backup.sh --apply        # snapshot the databases, then copy (no deletions)
#   scripts/backup.sh --apply --verify   # ... and then a full checksum comparison (slow: about an hour)
#
# What it does, in order:
#   1. Takes dated, consistent snapshots of the three SQLite files (index, edits, similar) into
#      <backup>/snapshots/ using SQLite's own backup, and checks each with integrity_check.
#   2. rsync's the archive to the backup disk. It NEVER deletes anything on the backup
#      (no --delete), so files moved to quarantine or parked stay in the backup under their old
#      paths until you decide to prune it. Excludes thumbs/, takeout-unpacked/, takeout-zips/.
#   3. Writes LAST_BACKUP.txt on the backup disk and a log in ~/backup-<timestamp>.log.
#   4. With --verify: an rsync checksum dry run (-avhnc); it must list nothing.
#
# Override locations with SRC= and DST= if the disks are mounted elsewhere.
set -euo pipefail

SRC="${SRC:-/media/aj9/Juniper13/photo-archive}"
DST="${DST:-/media/aj9/Juniper12/photo-archive-backup}"
APPLY=0; VERIFY=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --verify) VERIFY=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (try --help)"; exit 2 ;;
  esac
done

[ -d "$SRC/originals" ] || { echo "Source not found or not mounted: $SRC"; exit 1; }
[ -d "$DST" ] || { echo "Backup disk not found or not mounted: $DST"; exit 1; }
[ -f "$DST/README.txt" ] || { echo "No README.txt in $DST: is this the right backup folder? (create it if this is a new backup)"; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"; DAY="$(date +%F)"
LOG="$HOME/backup-$STAMP.log"
EXCL=(--exclude 'thumbs/' --exclude 'takeout-unpacked/' --exclude 'takeout-zips/')
RS=(rsync -avh --partial "${EXCL[@]}")

if [ "$APPLY" = 0 ]; then
  echo "DRY RUN (add --apply to do it). Free space on the backup disk:"
  df -h "$DST" | tail -1
  echo
  "${RS[@]}" -n --stats "$SRC/" "$DST/" | tail -25
  exit 0
fi

exec > >(tee -a "$LOG") 2>&1
echo "Backup $STAMP: $SRC -> $DST"

echo; echo "1. Database snapshots"
mkdir -p "$DST/snapshots"
python3 - "$SRC" "$DST/snapshots" "$DAY" <<'EOF'
import sqlite3, sys, os
src, out, day = sys.argv[1:4]
bad = 0
for name in ("curator", "curator_edits", "curator_similar"):
    p = os.path.join(src, name + ".sqlite")
    if not os.path.exists(p):
        print(f"   {name}: not present, skipped"); continue
    dest = os.path.join(out, f"{name}.{day}.sqlite")
    a = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    b = sqlite3.connect(dest)
    a.backup(b); b.commit()
    r = b.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"   {name}.{day}.sqlite: integrity {r}")
    bad += (r != "ok")
    a.close(); b.close()
sys.exit(1 if bad else 0)
EOF

echo; echo "2. Copying the archive (nothing is deleted on the backup)"
"${RS[@]}" --stats "$SRC/" "$DST/" | tail -20

echo; echo "3. Checking the copy"
LEFT="$("${RS[@]}" -n "$SRC/" "$DST/" | grep -vE '^(sending|sent |total size|building|$)' | grep -vE '/$' | wc -l || true)"
echo "   files still different by size/time: $LEFT"
printf 'Last backup: %s\nFrom: %s\nVerified by checksum: %s\n' "$(date)" "$SRC" "$([ "$VERIFY" = 1 ] && echo pending || echo no)" > "$DST/LAST_BACKUP.txt"

if [ "$VERIFY" = 1 ]; then
  echo; echo "4. Full checksum comparison (slow)"
  DIFF="$("${RS[@]}" -nc "$SRC/" "$DST/" | grep -vE '^(sending|sent |total size|building|$)' | grep -vE '/$' | wc -l || true)"
  if [ "$DIFF" = 0 ]; then
    echo "   VERIFIED: every file matches by checksum"
    sed -i 's/pending/yes, all files matched/' "$DST/LAST_BACKUP.txt"
  else
    echo "   $DIFF file(s) differ. Run: ${RS[*]} -nc $SRC/ $DST/"
    sed -i 's/pending/NO, differences found/' "$DST/LAST_BACKUP.txt"
    exit 1
  fi
fi

echo; echo "Done. Log: $LOG"
df -h "$DST" | tail -1
