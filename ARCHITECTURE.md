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
| Apple ingest: `scripts/apple_pull.sh` using `icloudpd` | Written; authentication done; still blocked on Apple's library indexing (wait loop left running in case it clears) |
| Old folder + Photos library export (`osxphotos` on the Mac) | Done: copied to `originals/` on the server |
| Archive scanner: `scripts/scan_archive.py` into `curator.sqlite` | Built and run on the real archive (4,086 files, 673 Live Photo pairs, 4 exact-duplicate groups) |
| Browse app (`app/`): grid, filters, detail with Live video, duplicates page, cached thumbnails | Built and running on junipernine2; Live badges, Live video playback and CR2 thumbnails confirmed by Tony |
| Manual date corrections (single photo or folder) | Built; tested on sample data |
| Near-duplicate finder with sharpness and burst labels (`scripts/find_similar.py`, `app/similar.py`, Similar page) | Built. Trial on 300 photos gave 75 groups, all bursts; full run in progress |
| Apple originals via the Mac (Photos download + `osxphotos`) | Started 9 Oct 13:30: the Mac's new system library is downloading originals from iCloud |
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

Beside `originals/` (all outside the repo):

| File or folder | What it is | Can it be rebuilt? |
|---|---|---|
| `curator.sqlite` | The scan index (`files`) | Yes, by re-running the scan |
| `curator_edits.sqlite` | Your date corrections | **No: back it up** |
| `curator_similar.sqlite` | Fingerprints and similar groups | Yes, by re-running `find_similar.py` |
| `thumbs/` | Cached thumbnails and previews | Yes, made on demand |

Other folders under `originals/`: `photos-catchall/` (an untouched copy of the old big
folder), `exports/photos-library-copy/` (an `osxphotos` export of an old library), and
`_apple-libraries-raw/` (raw `.photoslibrary` packages, parked and not indexed).

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

### Archive scanner (built)

`scripts/scan_archive.py` walks `originals/` and writes one row per photo or video into the
`files` table of `curator.sqlite`. It never changes a photo.

- **Skips:** any folder whose name starts with `_`, `.photoslibrary`-style packages, hidden
  files, and sidecar or thumbnail files (XMP, AAE and similar).
- **Reads:** metadata in batches with `exiftool` (JSON), and a SHA-1 of each file.
- **Capture date and where it came from** (`date_source`), best first: `exif` (camera),
  `folder` (a date in the folder name such as `2013_02_10`), `mtime` (file modification time,
  the least reliable: copying or exporting resets it).
- **Live Photo pairing** (`pair_key`): first by Apple's content identifier shared by the still
  and the video, otherwise a photo and a video with the same name in the same folder. `icloudpd`
  names a Live clip `IMG_1234_HEVC.MOV` beside `IMG_1234.HEIC`, so a video name ending in
  `_HEVC` is matched to the still without it. Pairing is rebuilt on every scan.
- **Resumable:** unchanged files (same size and modification time) are skipped on re-runs.
- **Report:** counts, exact-duplicate groups, Live pairs, sidecars, date sources.

`files` columns: `id`, `path` (relative to `originals/`), `kind` (photo or video), `ext`,
`size`, `mtime_ns`, `sha1`, `taken_at`, `date_source`, `width`, `height`, `make`, `model`,
`content_id`, `pair_key`, `has_sidecar`, `scanned_at`.

Results on the real archive: 4,086 files (3,337 of them photos), 673 Live Photo pairs, 4
exact-duplicate groups, and 223 files dated only by file modification time.

### Browse app (built)

`app/` is a FastAPI + Jinja2 + HTMX app (HTMX is vendored in `app/static/`, so it works with
no internet). Start it with `uvicorn app.main:app --port 8090` (commands in the runbook).

**Pages and routes**

| Route | What it does |
|---|---|
| `/` | "My photos": the thumbnail grid, 96 per page with a "Load more" button. Leaves out anything under `apple-shared/` |
| `/shared` | "Shared library": the same grid and filters, but only photos under `originals/apple-shared/` (the iCloud Shared Photo Library). Year counts and totals cover that tab only |
| `/photo/{id}` | Large preview, Live Photo video, metadata, identical copies, similar photos, date-fix box |
| `/dups` | Groups of byte-identical files (same SHA-1 and size), up to 200 groups, largest first |
| `/similar` | Near-duplicate groups (see below), 20 per page. Parameters: `kind` (`burst`, `spread`, `unknown`), `sort` (`space`, `size`), `group` (a group number), `page` |
| `/thumb/{id}`, `/preview/{id}` | Cached JPEGs, 360px and 1600px on the long side |
| `/file/{id}` | The original file, streamed unchanged (used by "Download original") |
| `POST /photo/{id}/date` | Saves a manual date (see below) |

The Duplicates and Similar pages and a photo's own page cover everything, shared photos
included, and show the file path so the source is clear. Keeping the shared library in its own
folder and tab is what lets us later present "only my photos" without other people's.

**Grid filters** (all combine, and all work as URL parameters): `year`, `kind` (photo or
video), `live=1` (Live Photos only), `q` (substring of the path, case-insensitive), `src`
(date source: `exif`, `folder`, `mtime`, or `manual`), `sort` (`new` or `old`), `page`.

**Behaviour**

- A Live Photo's video is hidden from the grid; it is shown inside its still's page. Tiles
  have VIDEO or LIVE badges and the capture date. Photos dated only by file date show the
  date in amber with a `?`.
- Thumbnails are made the first time they are needed and cached in `photo-archive/thumbs/`
  (`thumb` or `preview`, then `id/1000`, then `id.jpg`). HEIC is read with pillow-heif;
  RAW files (CR2, CR3, DNG, NEF, ARW, ORF, RW2) use the preview JPEG embedded in the file
  via `exiftool`; videos use one frame from `ffmpeg`. A file that cannot be read leaves a
  `.fail` marker so it is not retried every time, and the page shows a grey tile with the
  file type instead.
- iPhone Live Photo videos are HEVC. Safari plays them; Chrome and Firefox may not (the page
  offers a download link).
- The index is opened read-only. The app never modifies, moves or deletes an original.
- Paths from the index are resolved and must stay inside the archive (otherwise 403); a
  missing file gives 404; a missing index gives 503 with the command to run.
- **There is no login.** Anything on the home network that can reach the port can see the
  photos. Use `--host 127.0.0.1` to limit it to the server, and put Cloudflare Access in
  front before it is ever exposed outside the house.
- The one write action (setting a date) refuses requests that arrive from a different web
  site (an `Origin` or `Referer` check). This is a light guard, not authentication.

### Manual date corrections (built)

For photos whose date is wrong or only a guess (mostly the `mtime` ones).

- On a photo's page, **Fix the date** sets one date. A Live Photo's still and video always
  change together.
- **Set for the folder** applies one date to the files directly inside that photo's folder
  (not sub-folders). By default it only touches files dated from file date that have no
  correction yet; untick the box to change everything in the folder. The page shows the
  folder and the counts before you press the button.
- A date entered without a time is stored as 12:00:00. Dates before 1900 or after next year
  are refused.
- **Undo my date for this photo** removes the correction for that photo (and its Live partner).
- Corrections are stored in a separate file, `curator_edits.sqlite`, table `date_override`
  (`path`, `taken_at`, `prev_taken_at`, `prev_source`, `set_at`). The scan index and the
  photos are never changed, and a re-scan cannot undo a correction. Corrected photos use
  the new date everywhere (year filter, sort) and show as `manual` in the date-source
  filter. This file holds your decisions and has no other copy: back it up.

### Near-duplicate finder (built)

`scripts/find_similar.py` (logic in `app/similar.py`) finds photos that look alike but are
not byte-identical. It never changes a photo.

1. **Fingerprint:** each photo's cached 360px thumbnail is shrunk to 9x8 greyscale and
   compared pixel by pixel with its right-hand neighbour, giving a 64-bit "difference hash".
   Resized, re-saved and lightly edited copies differ by only a few bits. Blank or very
   dark pictures are marked `flat` and skipped (they would all match each other). Only
   photos are fingerprinted; a Live Photo's video is not.
2. **Sharpness score:** the variance of a Laplacian (edge) filter on the same thumbnail.
   Blurred frames score low. It is only meaningful between photos of the same scene, and a
   noisy or brightened copy can score high.
3. **Grouping:** two photos are candidates if they differ by at most `--threshold` bits
   (default 5, maximum 7, so that an eight-way split of the hash guarantees a shared slice).
   Pairs are merged closest-first, and two groups only join if every cross pair is within
   threshold + 3 bits, which stops chains of "A looks like B looks like C" from swallowing
   unrelated photos. Very crowded buckets (over 400) are skipped and counted in the report.
   Grouping 50,000 hashes took about 4 seconds in testing.
4. **Left out:** groups whose files all have the same SHA-1 (the Duplicates page covers
   those).
5. **Suggestions, never decisions:** each group marks its **Sharpest** photo (best for
   bursts) and its **Largest** (most pixels, then bytes; best for copies). The header says
   how far apart the camera times are ("within seconds", "over 2 h", "over 3 days") and how
   much space the non-largest photos use. Groups are listed with the most spare space first by default.

Data: `curator_similar.sqlite` (derived, safe to delete and rebuild) with tables `hashes`
(`path`, `size`, `mtime_ns`, `dhash`, `sharp`, `status` ok/flat/failed), `sim_group`,
`sim_info` and `sim_meta`. Re-runs only fingerprint new or changed photos, and add
sharpness to photos fingerprinted by an earlier version. Lowering `--threshold` regroups
from the stored fingerprints with no re-reading of photos.

Known limits: heavy crops and rotated copies are not matched (a missed match is safe);
bursts group together by design; the sharpness score is a simple measure, not a judgement
of the picture. On the first 300 photos of the real archive it produced 75 groups, all
bursts.

### Apple originals via the Mac (in progress)

`icloudpd` depends on Apple's web index, which has not finished. The alternative is the Mac
itself (a Mac mini): Photos downloads full originals, then `osxphotos` exports them and
`rsync` copies them to the server, as was done for the old libraries.

- iCloud Photos only syncs with the **System Photo Library**. Photos' library chooser does
  not mark which one is the system library; the Mac had many old libraries (2018 to 2022)
  and one new one.
- **Never make an old library the system library with iCloud Photos on:** its contents would
  upload into your iCloud library.
- Switching the system library turns iCloud Photos off for the old one and clears
  not-fully-downloaded items from the Mac only; iCloud and the iPhone are untouched.
- The first switch hung, then reported "User is changing the system photo library", then
  "The library could not be opened (3143)". Fix used: quit, restart the Mac, hold Option
  while opening Photos, create a new library, use it as the system library, then turn on
  iCloud Photos and **Download Originals to this Mac**.
- The Mac must not sleep while it downloads (`caffeinate -d`, or the energy setting that
  prevents sleep when the display is off).

### Clearing iCloud and Google (planned, gated)

Intent: keep a few recent photos in the cloud, pull weekly to the server, then clear
older ones from iCloud so the server is the home of the photos. Rules before anything is
removed:

- Photos is a sync service, not a backup: deleting on the Mac or iPhone also deletes from
  iCloud and every device.
- The archive must be scanned, counts matched against what Photos reports, and a second copy
  of the archive kept on a different disk.
- A review step lists exactly what is about to be removed and you approve it. Automatic
  delete options in tools (`icloudpd --auto-delete`, `--keep-icloud-recent-days`) stay off.

### Google ingest (planned)

Google Takeout zips go into `originals/google/` unopened. `immich-go` can read
the zips directly and uses Google's JSON sidecar files for dates and locations.
All parts of a Takeout must be present or metadata is lost.

### Immich (planned)

Store, browse, face recognition and CLIP search. Docker Compose on junipernine2
first (containers run directly on the Linux host, so the overhead that bothers
you on the Mac does not apply); a native install can be considered later.

### Curator app: later features (planned)

The browse app above is the start of this. Still to build: keep/reject marks (stored in the
edits file; mark and filter only), a review page listing everything marked before anything
is removed, picking the best frame from a Live Photo's video when the still is blurred,
Qwen captions and tags, and the Immich link. Every removal needs a human decision.

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
