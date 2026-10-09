# Photo Curator — Specification

*Draft v0.1 — 9 October 2026*

A self-hosted system for storing, tidying and presenting a lifetime of photos from Apple Photos, Google Photos and scanned prints, running on **junipernine2** alongside the recipe app.

---

## 1. Goals

1. **One home for every photo.** Apple Photos, Google Photos (Takeout) and scanned prints end up in a single library on junipernine2, off the phone and out of the cloud.
2. **Keep Live Photos, and use them.** Live stays on. For fast-moving subjects (the dogs, a fidgeting wife), pick the best frame from the 3-second video when the still is blurred.
3. **Tidy without losing anything.** Find duplicates, near-duplicates, bursts and poor shots, and propose what to keep. A human approves every removal.
4. **Clever local tagging.** A local Qwen vision model captions and tags photos, especially undated scans with no metadata. No photo leaves the house.
5. **Present it nicely.** Browsable by family on any device, behind Cloudflare Access, like recipes.junipernine.com.

### Non-goals (for now)

- Rewriting a full photo manager (thumbnails, maps, face recognition, mobile backup). Immich does those.
- Editing tools beyond choosing a Live frame and basic crop/rotate.
- Automatic deletion of anything, ever.

---

## 2. Architecture

```
  iPhone / Mac (Apple Photos)      Google Takeout zips      Scanner / folders
           │ osxphotos / icloudpd          │ immich-go             │ immich-go from-folder
           └──────────────┬────────────────┴───────────────────────┘
                          ▼
                 ┌──────────────────┐   API    ┌───────────────────────┐
                 │      Immich      │◄────────►│   Photo Curator (ours) │
                 │ store + viewer   │          │ FastAPI + SQLite + HTMX│
                 │ faces, CLIP, maps│          │ review queues, Live    │
                 └────────┬─────────┘          │ frame picker, scoring  │
                          │                    └──────────┬────────────┘
                          │                               │ HTTP
                          │                    ┌──────────▼────────────┐
                          │                    │ Ollama (Qwen-VL)      │
                          │                    │ RTX 4070, 12GB        │
                          │                    └───────────────────────┘
                          ▼
          Cloudflare Tunnel + Access (email allow-list, one-time PIN)
          photos.junipernine.com  → Immich
          curate.junipernine.com  → Curator (admin only)
```

### 2.1 Immich — the store and viewer

- Holds the originals (HEIC + MOV pairs kept together) and generates previews.
- Provides face recognition, CLIP smart search, maps, albums, sharing and phone apps.
- **Install:** Docker Compose on junipernine2 for the trial import (Linux containers run on the host kernel, without the VM overhead of Docker Desktop on the Mac). Revisit a native install (community `arter97/immich-native`, using the existing Postgres + pgvector) once the trial proves out.
- Photo files live on the large data disk; the database is backed up with `pg_dump`.

### 2.2 Photo Curator — our app

- Same stack and conventions as recipe-app: **FastAPI + SQLite + HTMX**, server-rendered, no JS build step.
- Talks to Immich only through its REST API with an API key. Never writes to Immich's database or files directly.
- Keeps its own SQLite database of scores, hashes, groups and decisions.
- Does the heavy work in background jobs (scoring, hashing, frame extraction, Qwen calls), and puts results into **review queues**.
- Writes approved outcomes back to Immich: tags, albums, stacks, favourites, archive, or trash (Immich's trash is recoverable).

### 2.3 Local AI

- Ollama on junipernine2, vision model `qwen2.5vl:7b` or `qwen3-vl:8b` (whichever fits 12GB VRAM alongside Immich ML; to be tested).
- GPU is shared with Immich's machine-learning service. Large Qwen batches run when Immich isn't indexing; the curator's job runner has a "pause AI jobs" switch.

---

## 3. Ingest

| Source | Tool | Where it runs | Notes |
|---|---|---|---|
| Apple Photos library | `osxphotos export` | Mac | Keeps albums, favourites, edits, people; exports HEIC + MOV pairs |
| iCloud (alternative) | `icloudpd` | junipernine2 | Linux-native; pulls straight from iCloud |
| Google Photos | Google Takeout → `immich-go upload from-google-photos` | junipernine2 | Process **all** Takeout parts together; uses JSON sidecars for dates/locations; pairs Live Photos |
| Scanned prints | Scanner → folder → `immich-go upload from-folder` | junipernine2 | No EXIF dates; Qwen proposes a date range and caption |

**Trial first:** one Takeout part + a few hundred iPhone photos. Check Live pairing, dates, albums and duplicate counts before the full import.

**Freeing iCloud space** is a separate, manual step once the archive is verified. Server copies do not free iCloud storage on their own.

---

## 4. Features

### F1. Live Photo frame picker *(first feature)*

The still stays the master; a chosen frame is an *addition*, never a replacement.

1. Extract all frames from the MOV with `ffmpeg` (~90 frames at 30fps) to a temp folder.
2. Score each frame for sharpness (OpenCV Laplacian variance) and keep the top 6.
3. Optionally ask Qwen-VL to rank the shortlist ("eyes open, face visible, subject sharp").
4. Review page: original still beside the candidate strip; click a frame to choose it.
5. Save the chosen frame as a new JPEG (carrying the original's EXIF date/GPS) and upload it to Immich **stacked** with the original.

Known limit: MOV frames are about 1920×1440, well below the 12–48MP still. Good for screen and 6×4 prints, not big crops.

**Which Live Photos to offer?** Only those whose still scores below a sharpness threshold, plus any marked by hand.

### F2. Duplicate and near-duplicate review

- **Exact duplicates:** same SHA-1 (Immich already catches most of these on upload).
- **Near duplicates:** perceptual hash (`imagehash` pHash/dHash) within a Hamming-distance threshold, e.g. Google's compressed copy vs the iPhone original.
- **Bursts / similar moments:** photos within N seconds of each other with similar hashes.
- Each group gets a proposed keeper (highest resolution → sharpest → best exposure → Qwen's pick).
- Review page: the group side by side; approve the proposal, pick another keeper, or "keep all". Non-keepers go to Immich trash or archive, as chosen.

### F3. Quality scoring

- Sharpness, exposure (histogram clipping), resolution, and file size per asset.
- Feeds F1 and F2, and a "probably rubbish" queue (blurred pocket shots, screenshots, accidental floor photos).

### F4. AI captions and tags

- Qwen-VL produces a one-line caption and a small set of tags (people count, dogs, place type, occasion).
- For scans: an estimated decade from visual cues, flagged as an estimate.
- Tags are written to Immich as tags with a `ai:` prefix, so they're easy to tell apart and remove.
- Runs as a resumable batch, one album or one year at a time, with review before writing back. Same approach as the recipe extraction.

### F5. Live video housekeeping *(later)*

Optionally drop the MOV from Live Photos that are not favourites and not in any album, once you're happy. Off by default, and reported for review rather than done automatically.

---

## 5. Curator data model (SQLite)

```
asset           -- one row per Immich asset we've looked at
  immich_id TEXT PK, kind ('photo'|'live'|'video'), taken_at, width, height,
  sha1, phash, dhash, sharpness, exposure_score, caption, scored_at

live_frame      -- candidate frames from a Live Photo's video
  id PK, asset_id FK, frame_no, path, sharpness, ai_rank, chosen BOOL

dup_group       -- a set of assets believed to be the same moment
  id PK, reason ('exact'|'near'|'burst'), created_at, status ('open'|'resolved')
dup_member
  group_id FK, asset_id FK, proposed_keeper BOOL, distance

ai_tag
  asset_id FK, tag, confidence, model, created_at, written_to_immich BOOL

decision        -- audit log of every human choice; nothing changes without one
  id PK, asset_id FK, action ('keep'|'trash'|'archive'|'stack'|'choose_frame'|'tag'),
  detail JSON, decided_at

job             -- background work runner
  id PK, type, params JSON, status, progress, started_at, finished_at, error
```

---

## 6. Screens

1. **Dashboard:** counts per queue, job status, GPU pause switch.
2. **Live frame picker:** one Live Photo at a time; keyboard shortcuts for next/skip/choose.
3. **Duplicate groups:** one group at a time, side by side.
4. **Rubbish queue:** grid of low-score photos with "keep" / "bin".
5. **AI tag review:** captions and tags for a batch, editable before writing back.
6. **Jobs:** history and logs.

Day-to-day browsing happens in Immich; the curator is the admin workbench.

---

## 7. Guiding rules

- **Never auto-delete.** Every removal is a human decision, logged in `decision`, and goes to recoverable trash first.
- **Originals are read-only.** New derived files (chosen frames) are added alongside, never written over.
- **Everything local.** No photo is sent to a cloud service for processing.
- **Resumable batches.** Every job can stop and pick up where it left off.
- **Learning project.** Built by pairing, step by step, not written in one go. One-off scripts live in `adhoc_scripts/` (git-ignored).

---

## 8. Phases

| Phase | Outcome |
|---|---|
| 0 | Repo, spec, and standalone frame-picker script tried on a few dog photos |
| 1 | Immich installed on junipernine2; trial import; Live pairing and dates checked |
| 2 | Full ingest of Apple, Google and existing scans |
| 3 | Curator skeleton: FastAPI app, SQLite schema, Immich API client, job runner |
| 4 | F1 Live frame picker in the app |
| 5 | F3 scoring + F2 duplicate review |
| 6 | F4 Qwen captions and tags |
| 7 | Cloudflare Tunnel + Access for Immich and the curator |
| 8 | F5 Live video housekeeping, and freeing iCloud space |

---

## 9. Open questions

- Immich in Docker long term, or move to native after the trial?
- Which Qwen vision model fits alongside Immich ML in 12GB, and at what speed?
- Duplicate thresholds: what Hamming distance and burst window work on this library?
- Do non-keepers go to Immich trash or archive by default?
- Scanning workflow for physical prints: resolution, file format, and who does the scanning.
- Separate subdomains (photos/curate) or one, with the curator under a path?
