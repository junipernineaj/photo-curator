#!/usr/bin/env bash
# Pull originals from iCloud Photos into the archive using icloudpd.
# Copy-only: this script never deletes anything locally or in iCloud.
#
# Usage:
#   scripts/apple_pull.sh auth            # one-off login + 2FA
#   scripts/apple_pull.sh trial           # download the 20 most recent photos
#   scripts/apple_pull.sh full            # download everything (resumable)
#
# Needs ICLOUD_USER (your Apple ID email). Run `full` inside tmux.

set -euo pipefail

ARCHIVE="${ARCHIVE:-/media/aj9/Juniper13/photo-archive}"
DEST="$ARCHIVE/originals/apple"
COOKIES="${COOKIES:-$HOME/.icloudpd-cookies}"
: "${ICLOUD_USER:?Set ICLOUD_USER to your Apple ID email}"

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
  *) echo "usage: $0 {auth|trial|full}"; exit 2 ;;
esac
