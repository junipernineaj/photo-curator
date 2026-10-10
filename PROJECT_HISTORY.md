# Project history

A record of the conversation behind photo-curator: the prompts that shaped it and the replies. Routine back-and-forth and troubleshooting (GitHub linking, wrong browser account, typos) is left out; long pasted logs are summarised in [brackets].

# 2026-10-03

ME: Inspired by my recipe app in the JuniperNine Recipe App Project I want to create my own offline storage, curation and presentation solution for my photos from Apple Photos, Google Photos and various offline real photos from over the years. I am constantly taking pictures with my iPhone that are 'Live photos' and this blows my storage. The Google photos are also in need of a clean up - I'd like to create a mechanism for storing the photos - much like my recipe app, viewing them - tidying them and presenting them. One thing I want to consider before I start is whether there are libraries for resolving a Live Photo from Apple the way you do in the apple interface or whether i'd have to do this myself first. My intention is to host this on the same server as my recipe app - I have LOTS of space - I also intend to use something like Qwen to do some clever tagging or resolution of poor or duplicate images. Is this viable - and can you suggest a starting point - would it be similar to my recipe-app presentation?

Yes, it's viable, and your setup is well suited to it: plenty of disk, a 12GB GPU already running Qwen, and the Cloudflare Tunnel + Access pattern you already use for recipes. Before you start building, though, here's a suggestion that might save you months.

**Live Photos: you don't have to do this yourself**

A Live Photo isn't a special format. It's two files, a HEIC still and a roughly 3-second MOV, linked by a shared identifier in their metadata. To "resolve" one the way the Apple interface does, you keep the HEIC (or the edited key frame, if you changed it) and decide what to do with the MOV. Several existing tools handle this:

- **osxphotos** (Python, runs on a Mac) reads the Photos library directly. It can export stills only, skipping the Live video, and it understands edits, albums, favourites and people. This is the best way to export from Apple with the curation you've already done intact.
- **icloudpd** runs on Linux, so it could live on junipernine2. It pulls straight from iCloud and has options to skip the Live video or only keep it.
- **exiftool** pairs HEIC and MOV files by their content identifier, if you end up with loose files.
- For Google Takeout, which scatters metadata into JSON sidecar files, **immich-go** uses Google Photos' date and location instead of EXIF data and handles recognising iPhone Live photos even when the filename is duplicated, e.g. IMG_2710(1).MP4 and IMG_2710(1).HEIC.

To stop the storage problem at the source, you can turn Live off by default on the iPhone. The setting to make it stick is under Settings → Camera → Preserve Settings.

**Consider Immich as the engine, not something you build**

Immich is a self-hosted Google Photos replacement, and it already covers much of what you've described:

- ingests photos from iPhone, Google Takeout and folders of scans
- pairs Live Photos automatically
- does face recognition and CLIP "smart search" (e.g. "dog on a beach") on the GPU
- has a duplicate review screen
- has iOS and Android apps for ongoing backup

Rebuilding the hard parts yourself (HEIC decoding, thumbnails for 100k+ images, video, EXIF, maps) would be a big job before you even reach the interesting curation work.

**Where your own app fits**

I'd use a hybrid, which keeps the part you enjoy, writing your own FastAPI + SQLite + HTMX code:

1. **Immich is the store and viewer.** Everything gets ingested there, and it's the day-to-day browsing app for you and your wife.
2. **A "photo curator" app is yours.** It looks like recipe-app, talks to Immich's API and does the clever work:
   - finds near-duplicates with perceptual hashing (the Python `imagehash` library) and bursts by time window
   - scores blur and exposure (OpenCV's Laplacian variance is a simple blur check)
   - asks a Qwen vision model through Ollama (qwen2.5vl or qwen3-vl at 7–8B fits in 12GB) things like "which of these 5 is the best shot and why" or "caption and tag this scanned 1980s print"
   - writes the results back to Immich as tags, albums or stacks
3. **Review, never auto-delete.** This is the same rule you settled on for media_inspector. The curator proposes "keep this one, bin these four", and you approve each group, much like your one-book-at-a-time review for recipes.

So yes, the presentation would feel like recipe-app: a server-rendered HTMX review queue behind Cloudflare Access at something like photos.junipernine.com. The difference is that the heavy lifting of storage and viewing comes for free.

**Practical cautions**

- **Takeout is messy.** You need every part of the export, or the metadata goes missing. Photos with missing JSON files are a common problem, and the fix is to request another takeout. Takeouts also contain lots of duplicates, often the same photo at full resolution and in Google's compressed version.
- **GPU sharing.** Immich's machine learning and Ollama will compete for the 4070's 12GB. Run big Qwen batches when Immich isn't doing its first indexing pass.
- **Clean-up happens at the source.** Removing Live videos from iCloud itself means switching Live off on each photo in the Photos app (or deleting the photo once it's safely archived). Your server copy doesn't free up iCloud space on its own.

**A starting point**

1. Install Immich (Docker Compose is its standard route, even though you went native for Ollama). Import a small test batch: one Takeout part and a few hundred iPhone photos via osxphotos or icloudpd. Check that Live Photo pairing and dates come out right.
2. Once you trust the import, do the full ingest.
3. Then start the curator repo, with duplicate detection and a review page as the first feature, and add Qwen tagging after that.

Want me to sketch the curator's data model and the first duplicate-detection script, the same way we built recipe-app?

Sources:
- [immich-go releases](https://github.com/simulot/immich-go/releases)
- [immich-go issue #390](https://github.com/simulot/immich-go/issues/390)

ME: So I don't want to turn off Live Photo - it is very helpful for my fast moving black dogs - or my every fidgetting wife - so I do like to find the best picture from the 3 second Mov.  I was hoping there was an offline way to manipulate the HEIC in browser.  Is there a way to go native with Immich - I don't like how resource hungry Docker is on my mac.

Good news on both, with one caveat about the frames.

**Picking the best frame from the Live video**

This is very doable, and a good fit for your own app. Immich will store and play Live Photos, but as far as I know it has no "choose key photo" feature, so this is where your curator earns its keep.

The caveat: the MOV is much lower resolution than the still. On recent iPhones the video is around 1920×1440, against a 12–48MP HEIC. Apple's own "Make Key Photo" has the same limitation; it's just less obvious on a phone screen. So a frame from the video is a rescue for when the still caught the dog mid-blur. It won't beat a sharp still. It'll look fine on screen or as a 6×4 print, less so cropped or printed large.

A flow that fits your FastAPI + HTMX stack:

1. **Extract frames on the server.** `ffmpeg` pulls all ~90 frames (3 seconds at 30fps) from the MOV into a temp folder.
2. **Auto-shortlist.** Score each frame for sharpness (OpenCV's Laplacian variance), keep the top 5–6, and optionally ask Qwen-VL which has eyes open, the dog's face visible, and so on.
3. **Scrubber page.** Show the original still beside a strip of candidate frames. You click one, and HTMX posts the choice back.
4. **Save, don't replace.** Write the chosen frame as a new JPEG next to the original, keeping the HEIC and MOV untouched, and push it into Immich stacked with the original.

**HEIC in the browser**

Safari displays HEIC natively; Chrome and Firefox mostly don't. The simple fix is to have the server generate JPEG/WebP previews with `pillow-heif`, which is also what Immich does internally. Everything stays on your LAN, so it's all offline in the sense that matters; nothing goes to the cloud. If you ever want decoding done purely in the browser, `libheif-js` (a WebAssembly build) can do it, but server-side previews are simpler and faster.

The Live videos themselves (HEVC) play in Safari. For other browsers, extracting frames with ffmpeg, as above, sidesteps the problem.

**Immich without Docker**

First, I think the resource hunger you've seen on the Mac comes from Docker Desktop, which runs a whole Linux virtual machine. On junipernine2, which runs Linux, containers run directly on the host kernel with very little overhead. So Docker there may not be the burden you're picturing. The Mac only needs to run osxphotos for the export.

If you'd still rather go native, which fits how you moved Ollama off Docker:

- **It's unofficial.** Docker is still the only officially supported install method, and the issue asking for native support has been open for years.
- **arter97/immich-native** is the established community route. It provides helper scripts to install Immich without Docker, installing to /var/lib/immich and running as systemd services.
- **It uses your own Postgres with pgvector**, plus Redis and Jellyfin's ffmpeg build. As an ex-DBA you'd probably find that more comfortable than a database hidden inside a container.
- **Upgrades are the cost.** Immich releases often, and with a native install you rebuild on each upgrade and hope the scripts keep up. Some people settle on Podman as a middle ground: no Docker daemon, same compose files, official upgrade path.

My suggestion: try it in Docker on junipernine2 first for the test import, and watch memory and CPU there. If it still bothers you, moving to the native install later is straightforward, because your photos live on disk and the database can be dumped and restored.

Want me to start with the frame-extraction and sharpness-scoring script? It works on its own, before Immich is even installed, so you could try it on a few dog photos this weekend.

Sources:
- [arter97/immich-native](https://github.com/arter97/immich-native)
- [Immich discussion #1657: native deployments](https://github.com/immich-app/immich/discussions/1657)
- [Immich in LXC guide](https://www.github.com/loeeeee/immich-in-lxc)
- [Lemmy thread on Immich without containers](https://lemmy.dayl.in/post/201597/1567121)

ME: sounds like I really need to turn off live photo unless I'm photographing the dogs - I'm compromising the quality over convenience.

Not quite: leaving Live Photo on doesn't cost you any quality. The iPhone still takes the full-resolution HEIC exactly as it would with Live off; the 3-second video is captured alongside it as an extra. The only cost is storage, roughly doubling each photo's size.

The quality trade-off only happens when you choose a frame from the video instead of the still. So with Live on, you always have the full-quality still, plus a lower-quality backup if the still caught the dog mid-blur.

That means your choice is really about storage, and you have a few options:

- **Leave it on and let the server take the strain.** Once photos are archived on junipernine2, which has the space, you can remove them from the phone and iCloud. Your curator could also drop the MOV from anything that isn't a keeper, or where you've accepted the still.
- **Toggle it per situation.** Live on for the dogs and your wife, off for landscapes, food and documents. Turn on Settings → Camera → Preserve Settings → Live Photo so the camera remembers your last choice rather than resetting to on.
- **Use burst mode for the fastest moments.** Hold the shutter and drag left,

---

# Summary of actions to date (added 2026-10-09)

The 3 October entries above cover the original discussion: whether the project is viable, how Live Photos work, Immich vs building from scratch, and Docker vs a native install. The log below covers 9 October, when the project started.

## What we decided

- Immich stores and presents the photos; photo-curator (FastAPI + SQLite + HTMX, like recipe-app) does the tidying.
- Keep Live Photo switched on. The still is always full quality; the 3-second video is only a backup source of frames.
- Copy only: nothing is deleted from iCloud, Google or the server until the archive is verified.
- Originals live outside the repo at `/media/aj9/Juniper13/photo-archive/`, replacing the earlier `/data/photos-staging` idea.

## Timeline

| Time (9 Oct) | Action | Outcome |
|---|---|---|
| 08:41 | Wrote `SPEC.md`, `README.md`, `.gitignore`; first local commit | Done |
| 08:46 | Agreed the working model: repo on GitHub, cloned on junipernine2, Claude commits, Tony pulls | Done |
| 08:58 | Tried to attach the GitHub repo to the session | Blocked: GitHub not linked to this session |
| 09:03 to 09:12 | Worked out why the GitHub connection prompted for onboarding | Cause: the connect link opened in a Chrome profile signed in to the wrong Claude account. Fixed by signing in to the right account |
| 09:12 | Attached `junipernineaj/photo-curator` | Read worked; push refused (app lacked write access). The repo also had a starter README and `.gitignore`, so the local commit was rebased on top |
| 09:14 | Pushed the spec after the Claude GitHub App was given write access | Done |
| 09:16 | Chose to start by pulling all photos from Apple and Google | Apple first |
| 09:22 | Confirmed the photos are in iCloud (about 30GB of the 43.85GB used); advanced data protection off; Juniper13 has 2TB+ free | Route: `icloudpd` on junipernine2 |
| 09:25 | Added `docs/INGEST.md` and `scripts/apple_pull.sh` (auth, trial, full) | Pushed |
| 09:29 | Installed `icloudpd` in a Python venv on junipernine2 | Done |
| 09:35 | First `auth` failed: no desktop keyring on the server | Fixed by using console password and 2FA prompts; script updated and pushed |
| 09:38 | `auth` succeeded with password and 2FA code | Session saved |
| 09:39 | `trial` stopped: Apple said iCloud setup was not complete (an updated-terms flag was set) | Fix: log in at icloud.com, accept the terms, open Photos |
| 09:43 | `trial` reached the library, then reported it had not finished indexing | Waiting on Apple; retry in 15 to 30 minutes |
| 09:46 to 09:55 | Committed `PROJECT_HISTORY.md`; wrote `ARCHITECTURE.md` and `RUNBOOK.md` | Pushed |

## Challenges overcome

1. **GitHub link prompting as if new:** wrong browser profile. Signed in to the right account.
2. **Push refused:** the Claude GitHub App needed read and write on the repo.
3. **Repo not empty:** GitHub added a README and `.gitignore`; rebased around them.
4. **Keyring error on a headless server:** use console password and MFA providers.
5. **iCloud "setup not complete":** accept the updated terms at icloud.com.

## Still open

- First successful `trial` download and Live Photo pairing check (blocked on indexing).
- Full Apple pull and count verification.
- Google Takeout request and download.
- Immich install, then the curator app and Qwen tagging.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [RUNBOOK.md](RUNBOOK.md) for how it works and how to run it.

## Later on 9 Oct: what was built and what happened

Clock times are only given where they were noted at the time.

**Archive and scan.** The old big folder (24GB) and an `osxphotos` export of an old Photos
library were copied to the server (`originals/photos-catchall`, `originals/exports/...`), and
the raw `.photoslibrary` packages were parked in `_apple-libraries-raw/` so they are not
indexed. `scripts/scan_archive.py` indexed 4,086 files. The first run found 0 Live Photo
pairs because of a pairing bug; after the fix it found 673. It also found 4 exact-duplicate
groups and 223 files dated only by file modification time.

**Google.** A Google Takeout export was requested at 11:11; it is waiting for Google's email.

**Browse app.** A read-only web app over the index (grid, filters, photo page with Live video,
exact duplicates) was written, tested on made-up data and pushed. Run on junipernine2 it
showed Live badges, played Live videos and showed thumbnails for the 72 CR2 files. One bug
found in use (a SQLite thread error when closing the connection) was fixed and confirmed gone.

**Date fixes.** Tony asked how to see the 223 file-dated photos; the date-source filter already
did this (I had wrongly said it was missing). Added an amber `?` on those tiles and a manual
date correction (one photo or a whole folder), saved in a separate `curator_edits.sqlite`.

**Apple download.** At 12:59 `icloudpd` was still waiting on Apple's indexing. Tony chose to
pull originals through the Mac instead and left the wait loop running. Switching the Mac's
system photo library hung, then gave "User is changing the system photo library", then error
3143 on reopening. A restart, then a new library made the system library, fixed it. By 13:30
iCloud Photos was on with "Download Originals to this Mac" and photos were arriving.

**Deletion plan.** Tony's aim is to keep a few photos in iCloud, pull weekly to the server and
clear the rest. Agreed that clearing is gated: archive verified, a second copy on another
disk, and an approved review list, because deleting in Photos deletes from iCloud and the
iPhone too.

**Near-duplicates.** Added `scripts/find_similar.py` and a Similar page (difference-hash
fingerprints, sharpness score, burst time span, Sharpest and Largest suggestions). The first
trial on 300 photos at 13:42 gave 75 groups, all bursts, which led to adding the sharpness
score and burst labels. The full run was started after that.

**Challenges overcome in this stretch:** Mac `pip` pointing at a missing Python (used a
venv); osxphotos dry run on the wrong library; missing destination folders for osxphotos and
rsync; the Live Photo pairing bug; the SQLite thread error; the Photos system-library switch
failure.

**Still open:** Apple's indexing (blocking `icloudpd`); the Mac download, export and rsync;
the full similar-photo run and review; keep/reject marks; Google Takeout download; Immich;
Qwen tagging; Cloudflare Tunnel + Access; a nightly backup of the SQLite files, in particular
`curator_edits.sqlite`.

## 10 Oct: everything in one place

**Where things stood.** By the evening of 9 Oct the iCloud pull (own and shared libraries) was
done and indexed. Over 9 and 10 Oct three more sources came in and Google arrived.

**Old folders brought in.** `/media/aj9/Juniper13/Pictures` (53 GB) was copied into
`originals/photos-catchall/Pictures-juniper13` (rsync with a checksum dry run afterwards: clean).
Its three `.photoslibrary` packages were handled by looking inside first: the two iPhoto
libraries were identical (1,197 originals each), so one was copied (`originals/` only);
the Photos library (868 originals) was a separate, smaller library and was copied too.
`PersonalPhotos/AGGREGATED-Photos` on Juniper12 (28,233 files, 75 GB, flat folder of Android and
Pixel era photos) was copied into `Personal-Photos-aggregated`. The home movies folder next
to it (2,454 files, partly short clips, at least one work recording) was left alone.
Retirement rules for source folders were written into the RUNBOOK: prove the copy, make a
second copy of the archive on another disk, decide about packages, only then delete, with
Tony's approval. **The backup to Juniper12 was deliberately deferred until everything is in
one place.** Nothing has been deleted anywhere.

**Dates.** Bulk copies had reset file dates (5,831 files dated 2021 or 2022 were really
collection days). Added a `filename` date source (Android, WhatsApp, burst names and the Unix
timestamps in Facebook and Snapchat names) and `scan_archive.py --redate`. 1,754 files now
take their date from the name. About 4,300 files remain dated only by file date; 2,964 of
them are tiny (about 290 x 330 pixel) Photos thumbnails, probably from the aggregated folder,
which are to be sorted out after the Google import (those with a full-size twin proposed for
rejection, the rest parked). Reading dates from the Photos library database for 384 further
files was judged not worth it (the names in it are UUIDs and the dates look like import dates).

**Fixes found on the way.** 92 Pixel burst cover JPEGs were 37 bytes short and failed
thumbnailing; thumbnails now read truncated JPEGs and `find_similar.py` retries failures
(clear the `.fail` markers first).

**Google Takeout.** Four zips (84,233 entries, about 188 GiB) were downloaded on the Mac,
copied to `photo-archive/takeout-zips/`, verified with `unzip -tq`, and unpacked into
`takeout-unpacked/`. `scripts/import_takeout.py` (dry run by default) matches photos to their
JSON sidecars and moves them into `originals/google/`. The real data needed three rules a fake
test had not shown: truncated and numbered sidecar names, album folders that name sidecars
without the extension, and the video halves of Live and motion photos (about 2,900 files),
which have no sidecar and use their still's. After those, 44,653 of 44,678 media files matched
(25 without a sidecar). The scanner reads the sidecar date (`date_source = google`) for photos
with no camera date; 4,423 files took it. Album copies identical to a year-folder photo (216)
were skipped and left in staging. Import took the archive to 98,208 files and 364.8 GB.

**What the import showed.** Exact duplicates rose from 2,912 to 28,235 groups: 29,255 spare
copies, 95.3 GB. 22,512 of the 28,233 aggregated files are byte-identical to Takeout files
(so the aggregated folder was an earlier Google Photos download), 2,523 Takeout files match the
iCloud shared library and 156 the own iCloud library (iPhone photos were also backed up to
Google). About 18,500 of the 44,462 Google files are genuinely new. The Takeout copy has the
sidecar, so it is the one to keep in any duplicate clean-up; about 5,700 aggregated files
are not in the Takeout, so that folder cannot be dropped wholesale.

**Scaling fix.** At 91,000 photos the similar-photo grouping skipped 742 crowded buckets
(was 55) because one-byte slices put about 350 photos in an average bucket. On skewed test
data it found only 54% of planted pairs. Switched to threshold + 1 wider slices and a 2,500
limit (96% in the same test, as fast). On the real archive: crowded buckets 742 to 0, groups
9,545 to 9,900.

**Still open (10 Oct).** Sweep the other disks for photo folders not yet found; the backup of
`photo-archive` to Juniper12 and a tested restore; Tony's review of the Similar page (does
Sharpest pick the right frame?); keep/reject marks and a Review page with bulk accept for the
burst groups (not yet confirmed by Tony); a proposed (never automatic) removal list for exact
duplicates; the 2,964 tiny thumbnails; retiring Pictures and the aggregated folder after the
backup; the iCloud and Google clearing plan; Immich, Qwen tagging, Cloudflare Tunnel + Access.

**Disk sweep (10 Oct, afternoon).** A read-only image count per top-level folder on Juniper10 to 13
found no further photo collections. The hits were album art (Flac-Music, Music-MP3), Plex's
PhotoTranscoder cache, and scratch pages from the OCR work (`Juniper13/tmp/ocrmypdf.*`). Juniper11
holds only Movies (9.5 TB). The photo sources are therefore complete: iCloud (own and shared),
Pictures, AGGREGATED-Photos and Google.

**First backup started (10 Oct, 13:58).** `rsync -avh --partial` of `photo-archive` to
`Juniper12/photo-archive-backup`, excluding `thumbs/` (rebuildable), `takeout-unpacked/` (staging) and
`takeout-zips/` (contents already in `originals/google`; kept on Juniper13 as a spare). No `--delete`.
To be followed by an `rsync -avhnc` checksum dry run and a restore test.
