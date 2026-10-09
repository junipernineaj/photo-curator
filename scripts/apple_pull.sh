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
#   scripts/apple_pull.sh shared-trial    # iCloud SHARED Photo Library: 20 most recent
#   scripts/apple_pull.sh shared-full     # iCloud SHARED Photo Library: everything
#                                         # (needs SHARED_LIBRARY=SharedSync-XXXX, from
#                                         #  `icloudpd --list-libraries`; saved to
#                                         #  originals/apple-shared/, apart from your own)
#
# If a password or 2FA prompt is needed, run `auth` first (prompts can be hidden in wait mode).
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

# The shared library goes in its own folder so it never mixes with your own photos.
shared_args() {
  : "${SHARED_LIBRARY:?Set SHARED_LIBRARY to the SharedSync-... name shown by icloudpd --list-libraries}"
  local sd="$ARCHIVE/originals/apple-shared"
  mkdir -p "$sd"
  SHARED=(--directory "$sd" --username "$ICLOUD_USER" --cookie-directory "$COOKIES"
          --folder-structure "{:%Y/%m}" --library "$SHARED_LIBRARY"
          --password-provider console --mfa-provider console)
}

case "${1:-}" in
  shared-trial) shared_args; icloudpd "${SHARED[@]}" --recent 20 ;;
  shared-full)  shared_args; icloudpd "${SHARED[@]}" ;;
  auth)  icloudpd "${COMMON[@]}" --auth-only ;;
  trial) icloudpd "${COMMON[@]}" --recent 20 ;;
  full)  icloudpd "${COMMON[@]}" ;;
  wait)
    LOG="$(mktemp)"
    for i in $(seq 1 24); do
      echo "[$(date +%H:%M:%S)] attempt $i of 24"
      # show status lines only (hide the raw JSON debug dump); full output kept in $LOG
      icloudpd "${COMMON[@]}" --recent 20 2>&1 | tee "$LOG" | grep --line-buffered -vE '^[[:space:]]|^[{}]|DEBUG' || true
      # 1) still indexing (logged as INFO or ERROR depending on the attempt): keep waiting
      if grep -q "not finished indexing" "$LOG"; then
        :
      # 2) any other real error: stop
      elif grep -qE '^[0-9-]+ [0-9:]+ +ERROR|AUTHENTICATION_FAILED|locked for security' "$LOG"; then
        echo "An error (not just indexing) occurred - stopping. Read the output above."
        rm -f "$LOG"; exit 1
      # 3) neither: the trial ran
      else
        echo "No indexing message and no errors - check the output above, then run: $0 full"
        rm -f "$LOG"; exit 0
      fi
      echo "Still indexing; sleeping 20 minutes"; sleep 1200
    done
    rm -f "$LOG"; echo "Gave up after 24 attempts"; exit 1 ;;
  *) echo "usage: $0 {auth|trial|full|wait|shared-trial|shared-full}"; exit 2 ;;
esac
