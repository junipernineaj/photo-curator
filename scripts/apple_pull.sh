#!/usr/bin/env bash
# Pull originals from iCloud Photos into the archive using icloudpd.
# Copy-only: this script never deletes anything locally or in iCloud.
#
# Usage:
#   scripts/apple_pull.sh auth            # one-off login + 2FA
#   scripts/apple_pull.sh trial           # download the 20 most recent photos
#   scripts/apple_pull.sh full            # download everything (resumable)
#   scripts/apple_pull.sh wait            # retry the trial every 20 min until Apple's
#                                         # library indexing finishes (max 24 tries)
#
# Needs ICLOUD_USER (your Apple ID email). Run `full` inside tmux.

set -euo pipefail

ARCHIVE="${ARCHIVE:-/media/aj9/Juniper13/photo-archive}"
DEST="$ARCHIVE/originals/apple"
COOKIES="${COOKIES:-$HOME/.icloudpd-cookies}"
: "${ICLOUD_USER:?Set ICLOUD_USER to your Apple ID email}"
case "$ICLOUD_USER" in
  *example.com*|*YOUR-APPLE-ID*) echo "ICLOUD_USER is still a placeholder ($ICLOUD_USER). Set it to YOUR real Apple ID email."; exit 1 ;;
esac

command -v icloudpd >/dev/null || { echo "icloudpd not found - see docs/INGEST.md"; exit 1; }
mkdir -p "$DEST" "$COOKIES"

# console providers: ask at the terminal (this server has no desktop keyring).
# Files land in year/month folders. NEVER add --auto-delete or
# --keep-icloud-recent-days here: they delete files.
COMMON=(--directory "$DEST" --username "$ICLOUD_USER" --cookie-directory "$COOKIES"
        --folder-structure "{:%Y/%m}"
        --password-provider console --mfa-provider console)

case "${1:-}" in
  auth)  icloudpd "${COMMON[@]}" --auth-only ;;
  trial) icloudpd "${COMMON[@]}" --recent 20 ;;
  full)  icloudpd "${COMMON[@]}" ;;
  wait)
    LOG="$(mktemp)"
    for i in $(seq 1 24); do
      echo "[$(date +%H:%M:%S)] attempt $i of 24"
      icloudpd "${COMMON[@]}" --recent 20 2>&1 | tee "$LOG" | tail -n 15
      if grep -qE "ERROR|serviceErrors|locked|AUTHENTICATION_FAILED" "$LOG"; then
        echo "An error (not just indexing) occurred - stopping. Read the output above."
        rm -f "$LOG"; exit 1
      fi
      if ! grep -q "not finished indexing" "$LOG"; then
        echo "No indexing message and no errors - check the output above, then run: $0 full"
        rm -f "$LOG"; exit 0
      fi
      echo "Still indexing; sleeping 20 minutes"; sleep 1200
    done
    rm -f "$LOG"; echo "Gave up after 24 attempts"; exit 1 ;;
  *) echo "usage: $0 {auth|trial|full|wait}"; exit 2 ;;
esac
