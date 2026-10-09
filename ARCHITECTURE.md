# Architecture

How photo-curator fits together. For the full design and feature list see
[SPEC.md](SPEC.md); for commands and settings see [RUNBOOK.md](RUNBOOK.md).
This file separates **what exists today** from **what is planned**.

## The idea

Immich stores and presents the photos. Photo Curator (this repo) does the
tidying. Nothing is deleted automatically, and no photo leaves the house.

```
 iCloud Photos ──icloudpd──┐
 Google Takeout zips ──────┼──►  /media/aj9/Juniper13/photo-archive/originals/
 Scanned prints ───────────┘            │  (read-only master copies)
                                        ▼
                               Immich (store + viewer)          [planned]
                                        ▲
                                        │ REST API
                               Photo Curator (FastAPI + SQLite + HTMX)   [planned]
                                        │
                               Ollama + Qwen vision model        [planned]
```

## Status

| Piece | Status |
|---|---|
| Specification (`SPEC.md`) | Done |
| Apple ingest: `scripts/apple_pull.sh` using `icloudpd` | Written; authentication done; first download waiting on Apple's library indexing |
| Old folder + Photos library export (`osxphotos` on the Mac) | Done: copied to `originals/` on the server |
| Archive scanner: `scripts/scan_archive.py` into `curator.sqlite` | Written and tested on sample data; not yet run on the real archive |
| Browse app (`app/`): grid, filters, detail with Live video, duplicates page, cached thumbnails | Written and tested on sample data; not yet run on the real archive |
| Google Takeout ingest | Requested 9 Oct; waiting for Google |
| Immich on junipernine2 | Not started |
| Curator app (Live frame picker, duplicates, scoring) | Not started |
| Qwen tagging | Not started |
| Cloudflare Tunnel + Access for photos | Not started |

## Where things live

| What | Where |
|---|---|
| Code and docs | This repo (public): `github.com/junipernineaj/photo-curator`; cloned on junipernine2 at `~/photo-curator` |
| Photo data | `/media/aj9/Juniper13/photo-archive/` (outside the repo; never committed) |
| iCloud login session | `~/.icloudpd-cookies` on junipernine2 (a credential; not in the repo) |
| icloudpd program | Python venv on junipernine2 (the `(venv)` prompt) |
| Secrets (future) | `.env` on the server only; `.env` is git-ignored |

The data drive (Juniper13, 2TB+ free) is shared with the cookbook project, so
photo data sits in its own `photo-archive/` folder.

## Archive layout

```
/media/aj9/Juniper13/photo-archive/
  originals/apple/    icloudpd output, in year/month folders
  originals/google/   Takeout zips, kept untouched
  originals/scans/    scanned prints (later)
```

`originals/` is the clean master set and is never edited. Immich's own library
will live in a separate folder so the originals stay a pristine backup.

## Components

### Apple ingest (built)

`scripts/apple_pull.sh` wraps `icloudpd`, which logs in to iCloud as if it were
the iCloud web site and downloads the originals.

- **Why `icloudpd` rather than exporting from the Mac:** the photos are stored
  in iCloud, and the Mac only holds small previews of most of them (see the
  storage settings). Pulling from iCloud directly avoids making the Mac
  download everything first.
- **Output:** each Live Photo arrives as two files, a `.HEIC` still and a `.MOV`
  video with the same base name. Files are filed in `YYYY/MM` folders.
- **Copy only:** the script has no delete options. Never add `--auto-delete`
  or `--keep-icloud-recent-days`.
- **Console prompts:** the server is headless and has no desktop keyring, so
  the script uses `--password-provider console --mfa-provider console`.
- **Limit:** `icloudpd` gives files and dates. As far as we know it does not
  carry albums or favourites across. `osxphotos` on the Mac could add that later.

### Google ingest (planned)

Google Takeout zips go into `originals/google/` unopened. `immich-go` can read
the zips directly and uses Google's JSON sidecar files for dates and locations.
All parts of a Takeout must be present or metadata is lost.

### Immich (planned)

Store, browse, face recognition and CLIP search. Docker Compose on junipernine2
first (containers run directly on the Linux host, so the overhead that bothers
you on the Mac does not apply); a native install can be considered later.

### Photo Curator (planned)

FastAPI + SQLite + HTMX, same style as recipe-app. Talks to Immich only through
its API. Works through background jobs and review queues. First feature: pick the
best frame from a Live Photo's video when the still is blurred. Then duplicate
review, quality scoring and Qwen captions. Every removal needs a human decision.

### Local AI (planned)

Ollama on junipernine2 (RTX 4070, 12GB) with a Qwen vision model, sharing the GPU
with Immich's machine learning service.

## Dependencies

The full, current list of what must be installed on each machine is in
[RUNBOOK.md](RUNBOOK.md#dependencies). Keep it in one place; update it whenever a
new tool is added.

## Principles

- Copy only; originals are read-only; nothing is deleted without a human decision.
- Everything local; no photo goes to a cloud service for processing.
- Resumable batches.
- Photo data and credentials never go in the repo.
