# Feature backlog

Ideas and discussions for future work. Nothing here is built unless it says so.
Newest discussion first within each section. Move an item to PROJECT_HISTORY.md when it is done.

## Uploading from outside the server (discussed 11 Oct 2026)

Depends on the Cloudflare Tunnel + Access step being done first. Never expose an upload endpoint without a login in front.

**The idea:** treat the app like Dropbox's front door, but as an **Inbox**, not a sync. Files go in from several places and
land in one spot; the archive stays the source of truth.

Dropbox is four features bundled together; they have different costs here:

1. **Web upload / drag and drop.** Easy. A page that takes files, shows progress and a result per file.
   On arrival, checksum each file and say "already in your archive at ..." if it is a repeat.
2. **Phone "Share to..."**
   - Android: an installed web app can register as a share target.
   - iPhone: Safari does not support share targets (as far as I know). Use an iOS Shortcut that appears in the share
     sheet and posts the files to the upload address; it can ask "which folder?". One-off setup on the phone.
   - Cloudflare Access can't do a normal login from a Shortcut, so use a Cloudflare **service token** (a secret on the phone).
   - Share-sheet uploads can lose location data, and Live Photos may arrive as a separate still and video. The weekly iCloud
     pull stays the faithful route for the phone camera roll; uploads suit scans, other people's photos and one-off files.
3. **Camera-roll auto-upload.** Already covered by the planned weekly iCloud pull script. iOS limits background runs, so
   a Shortcut automation is possible but unreliable.
4. **Synced folder on a computer.** Deliberately NOT two-way sync: deletions flowing back would fight the
   quarantine-only rule. A one-way drop folder is a better fit, and on the home network an ordinary network share pointing at the
   same landing folder needs no new code.
5. **Other people.** Cloudflare Access knows who is logged in, so uploads can be tagged by person (an Inbox per person:
   Tony, wife, mum), plus an optional "shared with me" view. Share links are possible later.

**Design notes**
- Landing place: a new folder under originals (one per month, e.g. `uploads/2026-10/`), files added only, never overwritten
  (open mode "x", like Best Take), then the normal scan indexes them. A small, deliberate bend of "the web app only records
  decisions"; it is append-only. Folders starting with `_` are skipped by the scanner, so a `_` staging folder would need an
  explicit "file it" step.
- Destination choice: short dropdown ("Inbox", "Scans", existing folders).
- Cloudflare free plan caps a single request at **100 MB**. Fine for photos; long videos need chunked/resumable uploads.
- Safety: file-type allow-list, cleaned file names, size cap, disk-space check, rate limit, no execution of anything uploaded.
  Make sure `scripts/backup.sh` covers the landing folder.
- Build order: upload endpoint + landing folder + web page first; then see which of the Shortcut / drop folder / per-person
  Inboxes actually get used.

## Photo handling

- **Make the suggested keeper ignore eye data when a group looks unreliable** (e.g. more than ~70-80% of faces "closed", as in
  groups 8686, 3389, 8212 and anything in sunglasses).
- **Best Take, bigger scale:** batch run with an approve/reject page; tune mask and box (glasses, hair seams); check at full resolution.
- **Vision check:** try `qwen2.5vl:32b` if blink calls prove biased; speed-ups (currently ~7-10 s/photo with ~7 people).
- **Location carry-over** from a discarded photo to the keeper when the keeper has none.
- **Bulk-accept suggestions** for "same photo in several sources" groups, if wanted.
- **Live Photo frame picker.**
- **Review table of quarantine batches:** group by week if the list gets long.
- The 8 held Live Photo exact-duplicate groups (255 MB) are left alone for now.

## Archive and sources

- Album extraction from the iPhoto / Aperture / Photos libraries.
- Leads on the 2011-2013 gap.
- Retire old source folders only on Tony's say-so (Pictures and AGGREGATED-Photos verified safe for photos and videos; keep library packages).
- Purge of quarantine only after 30 days and with Tony's explicit go-ahead.
- Birmingham ~11k cluster on the Map: question still open.
- Weekly iCloud pull script; iCloud clearing (gated plan); Google clear-out separately.

## Presenting and tagging

- Cloudflare Tunnel + Access (consider `TILE_URL=none` so the map does not leak requests).
- Qwen tagging of photos; Immich evaluation.

## Operations

- Nightly SQLite `.backup` job.
- After purges, prune old paths from the backup.
- Refresh the offsite drive (still pre-cleanup).
- Server reboot at a quiet moment to clear the NVIDIA driver/library mismatch; then restart the photo app and re-run `vision_check` (resumable).
- Rebuild groups with `scripts/find_similar.py` once the vision job has finished (do not run both at once).
