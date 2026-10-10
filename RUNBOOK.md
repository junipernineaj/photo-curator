# Runbook

Commands, settings and fixes for running photo-curator. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together.

## Dependencies

The single list of everything that must be installed. Add to it whenever a new
tool is needed, and tick off what is installed.

### On junipernine2 (Ubuntu server)

| Needed | Why | How | Installed |
|---|---|---|---|
| `git` | Pull this repo | Same credentials as recipe-app | yes |
| Python 3 + `python3-venv` | Run icloudpd in an isolated environment; run `scan_archive.py` (standard library only) | `sudo apt install -y python3-venv` if `venv` fails | yes |
| `icloudpd` | Download from iCloud Photos | PyPI package in `~/venvs/icloudpd` (a standalone executable is also on the project's GitHub releases page) | yes |
| `tmux` | Keep long jobs running if SSH drops | `sudo apt install -y tmux` | yes (used for the wait loop) |
| `exiftool` | Read dates, sizes, camera info and Live Photo IDs in the scan | `sudo apt install -y libimage-exiftool-perl` | yes |
| `ffmpeg` | Video thumbnails in the browse app | `sudo apt install -y ffmpeg` | install |
| Browse-app Python packages (FastAPI, Uvicorn, Jinja2, Pillow, pillow-heif) | The browse page | venv `~/venvs/curator`; `pip install -r requirements.txt` | install |
| `sqlite3` (command line) | Inspect the index by hand; optional | `sudo apt install -y sqlite3` | check |
| Free disk | Photo archive | `/media/aj9/Juniper13` has 2TB+ free | yes |

### On the Mac (Sonoma, Intel)

| Needed | Why | How | Installed |
|---|---|---|---|
| Python 3 (3.11) | Run osxphotos | The system `pip` is broken (points at a missing Python); always use a venv | yes |
| `osxphotos` | Export originals, dates and metadata from old `.photoslibrary` packages | `python3 -m venv ~/venvs/osxphotos && ~/venvs/osxphotos/bin/pip install osxphotos`; put `~/venvs/osxphotos/bin` on PATH | yes (0.77.2) |
| `rsync` | Copy files to the server | Built in (old 2.6.9; works). Use `--partial` with two dashes | yes |
| ssh access to the server | rsync and `mkdir` on the server | Existing login to `aj9@junipernine2` | yes |

### Planned (not needed yet)

OpenCV and `imagehash` (frame picker, blur scores, near-duplicates); Immich with PostgreSQL + pgvector;
Ollama with a Qwen vision model; Cloudflare Tunnel + Access.

## Apple Photos: required settings

These must be right or the download fails.

| Setting | Required value | Where | Why |
|---|---|---|---|
| Advanced Data Protection | **Off** | iPhone: Settings > your name > iCloud > Advanced Data Protection | With it on, Apple refuses web access and `icloudpd` gets "access denied". (Currently off.) |
| Access iCloud Data on the Web | **On** | iPhone: Settings > your name > iCloud | `icloudpd` talks to iCloud the way the web site does |
| Two-factor authentication | Enabled (already) | Apple ID | Needed to log in; the script prompts for the code |
| iCloud terms / web setup | **Accepted at icloud.com** | Browser: https://www.icloud.com | See "First-time snags" below |

Library state: originals are in iCloud (iCloud Photos on), and the Mac only holds
optimised previews, which is why we pull from iCloud instead of exporting from the Mac.

## One-time setup

```
# 1. Get the repo
git clone https://github.com/junipernineaj/photo-curator.git ~/photo-curator
cd ~/photo-curator

# 2. Install icloudpd in a venv
python3 -m venv ~/venvs/icloudpd
~/venvs/icloudpd/bin/pip install icloudpd
export PATH=~/venvs/icloudpd/bin:$PATH      # add to ~/.bashrc to keep it
icloudpd --help | head -30

# 3. Tell the script which Apple ID to use
export ICLOUD_USER='YOUR-APPLE-ID-EMAIL'     # replace with your real Apple ID email
```

## Download from iCloud

```
scripts/apple_pull.sh auth     # once: asks for Apple password, then the 2FA code
scripts/apple_pull.sh trial    # the 20 most recent photos
```

Check the trial before the full pull:

```
find /media/aj9/Juniper13/photo-archive/originals/apple -type f | head -30
```

You want to see year/month folders, and each Live Photo as a `.HEIC` plus a
`.MOV` with the same base name. Then the full pull, inside tmux:

```
tmux new -s applepull
scripts/apple_pull.sh full
# detach: Ctrl-b then d      reattach: tmux attach -t applepull
```

It is resumable: running it again skips files already downloaded.

Verify afterwards by comparing file counts with the iPhone's Albums > Media Types
counts, and spot-checking a few photos from each year.

The login session lasts until Apple expires it (weeks, not days, in our
experience so far; not guaranteed). When it expires, run `auth` again.

## Why the download may not start straight away

Authentication and download are separate steps, and Apple can block the second
even after the first succeeds. We hit these in order on 9 Oct 2026.

### First-time snags

| Symptom | Cause | Fix |
|---|---|---|
| `KeyringLocked: Failed to unlock the collection` | The server has no desktop keyring, and icloudpd tried to fetch the password from it | Use console providers: `--password-provider console --mfa-provider console` (the script now does this) |
| `Apple iCloud setup is not complete. Please log into https://icloud.com/ ...` and the reply showed an updated-terms flag and no services | Apple had an updated iCloud terms notice waiting, and it withholds the Photos service until it's accepted in a browser | Log in at icloud.com, accept the terms, open Photos once, then re-run |
| `Apple iCloud Photo Library has not finished indexing yet` (sometimes logged as `ERROR library exception ...` followed by `Unknown library: PrimarySync`; same cause) | Apple builds a web-access index of the library the first time something other than the Photos app asks for it; with a library of this size it takes a while | Wait and retry (see below) |

### About the indexing wait

This is our understanding rather than something Apple documents precisely:
iCloud Photos has to prepare the library for web access before a client like
`icloudpd` can list it. Until that finishes, the tool can log in but sees
nothing to download. The time is up to Apple (anywhere from minutes to hours).

What to do:

- Open https://www.icloud.com/photos in a browser and leave it open; it signals when the library is ready, because your photos will load there.
- Wait 15 to 30 minutes between retries rather than re-running repeatedly.
- Don't change things in Photos on the iPhone in the meantime.
- `scripts/apple_pull.sh wait` retries the 20-photo trial every 20 minutes (up to 24 tries) and stops when the indexing message disappears. Run it in tmux.
- This version of icloudpd has no option to skip the indexing check, so waiting is the only route.
- Success looks like `Downloading ... IMG_xxxx.HEIC` lines instead of the indexing message.

## Scan the archive into an index

`scripts/scan_archive.py` walks `/media/aj9/Juniper13/photo-archive/originals/` and
records one row per photo or video in a SQLite file (`photo-archive/curator.sqlite`,
outside the repo). It is **read-only on the photos**: it only writes the index.

```
sudo apt install libimage-exiftool-perl      # needs exiftool
cd ~/photo-curator && git pull
scripts/scan_archive.py --limit 200          # try a small batch first
scripts/scan_archive.py                      # then the lot (resumable)
scripts/scan_archive.py --report             # summary only
```

What it records: size, SHA-1 (for exact duplicates), capture date and where the date
came from (`exif`, then the `filename` such as `IMG_20180619_220821.jpg`, then the `folder` name like `2013_02_10`, then file `mtime`),
dimensions, camera, Live Photo pairing, and whether an XMP/AAE sidecar exists.
It skips folders starting with `_`, `.photoslibrary`-style library packages, hidden
files, and sidecar/thumbnail files. Re-runs skip files that haven't changed.

Park raw Apple library packages in `originals/_apple-libraries-raw/` so they are
never indexed (the exported photos in `originals/exports/` are what we index).

Archive layout so far:

```
originals/
  photos-catchall/               untouched copy of the old folder (loose date folders etc.)
  exports/photos-library-copy/   osxphotos export from "Photos Library copy.photoslibrary"
  _apple-libraries-raw/          the raw .photoslibrary packages, parked, not indexed
  apple/                         icloudpd download (waiting on Apple's indexing)
  google/                        unpacked Google Takeout photos plus JSON sidecars (see "Google Takeout import")
```

## Apple Photos library export (osxphotos, on the Mac)

For old `.photoslibrary` packages. `osxphotos` is installed in a venv
(`~/venvs/osxphotos`; the system `pip` on the Mac points at a missing Python).

```
export PATH=~/venvs/osxphotos/bin:$PATH
osxphotos info --library "Photos Library copy.photoslibrary"     # counts, no export
mkdir -p ~/photo-export/copy                                     # DEST must already exist
osxphotos export ~/photo-export/copy --library "Photos Library copy.photoslibrary" \
  --directory "{created.year}/{created.mm}" --sidecar xmp --dry-run
```

Remove `--dry-run` for the real export. Notes from the first run (11GB library):
shared-album photos show as "missing" (no original in the library), and `--sidecar xmp`
writes a metadata file for every photo including those, so about two thirds of the
`.xmp` files have no image next to them. They're harmless; the scan ignores them.

Copy to the server (create the parent folders first, or rsync fails):

```
ssh aj9@junipernine2 'mkdir -p /media/aj9/Juniper13/photo-archive/originals/exports/photos-library-copy'
rsync -avh --partial --progress ~/photo-export/copy/ aj9@junipernine2:/media/aj9/Juniper13/photo-archive/originals/exports/photos-library-copy/
```

`-n` makes any rsync a dry run. Use `--partial` with two dashes.

## Apple originals via the Mac (alternative to icloudpd)

Use this while `icloudpd` is stuck on Apple's indexing. Background and reasons are in
[ARCHITECTURE.md](ARCHITECTURE.md#apple-originals-via-the-mac-in-progress).

1. Check free space on the Mac (`df -h ~`): about 45GB for the originals, plus the same again
   if you export to the Mac's own disk (export to the external SSD instead if you can).
2. Photos > Settings > General: the library shown must be the **System Photo Library**. If
   not, quit Photos, hold **Option** while opening it, choose or create the library you want
   (create a new empty one if the old ones are 2018 to 2022 libraries), then **Use as System
   Photo Library**. Do not use an old library for this.
3. Settings > iCloud: tick **iCloud Photos** and choose **Download Originals to this Mac**.
4. Keep the Mac awake: `caffeinate -d` in a Terminal tab. Watch "Downloading N items" at the
   bottom of the Library view, or the disk filling up.
5. When the count reaches zero, dry-run the export, then export and rsync as in the
   osxphotos section above (no `--library` flag means the system library; a few shared-album
   photos will show as missing, which is normal):

```
export PATH=~/venvs/osxphotos/bin:$PATH
mkdir -p ~/photo-export/icloud
osxphotos export ~/photo-export/icloud --directory "{created.year}/{created.mm}" --sidecar xmp --dry-run
```

Snags met on 9 Oct:

| Symptom | Cause and fix |
|---|---|
| Photos shows 1 photo and offers "Use as System Photo Library" | It is open on a library that is not the system one. Make the right one the system library |
| "Switching the System Photo Library will turn off iCloud Photos..." | Normal. Affects only this Mac's local copies; iCloud and the iPhone keep everything. Click OK, then turn iCloud Photos on in the new library |
| Spinning wheel after switching | Often just busy. Wait about 10 minutes, check Activity Monitor for CPU use by `Photos` and `photolibraryd` |
| "User is changing the system photo library" | The first switch is still running or stuck. Do not retry; quit, restart the Mac |
| "The library could not be opened (3143)" on reopen | The half-switched library is damaged. Restart, hold Option, create a new library, make it the system library |

Then clear iCloud only after the gated checks in the architecture notes. Deleting in
Photos deletes everywhere.

## iCloud Shared Photo Library

If you share a library with family, it is a separate iCloud library from your own
(`PrimarySync`). `icloudpd` only downloads one library at a time, so the shared one needs its
own run. List the libraries (read-only, downloads nothing):

```
~/venvs/icloudpd/bin/icloudpd --directory /tmp/icloud-probe --username "$ICLOUD_USER" \
  --cookie-directory ~/.icloudpd-cookies --password-provider console --mfa-provider console \
  --list-libraries
```

Yours printed `PrimarySync` and one `SharedSync-...` name (the name is not kept in this
repo). Then:

```
export SHARED_LIBRARY='SharedSync-...'       # exactly as printed
scripts/apple_pull.sh shared-trial           # 20 most recent, into originals/apple-shared/
scripts/apple_pull.sh shared-full            # everything, in tmux
```

Shared-library photos go to `originals/apple-shared/`, apart from your own, and appear in the
browse app under **Source > iCloud: shared library**. The scan indexes
them (the folder is under `originals/`). **Shared Albums** (albums other people share with you)
are different: `icloudpd` does not download them, and they showed as "missing" in the
`osxphotos` export. To keep some, use Add to Library in Photos on the Mac first.

### Re-reading dates from file names (`--redate`)

Files copied in bulk get the copy date as their file date, which is wrong (9 Oct: 5,831
files dated 2021 or 2022 that were not). Android and WhatsApp names carry the real date.
`scripts/scan_archive.py --redate` re-reads those for files dated only by `mtime` or
`folder`, in seconds, without re-scanning any photo. Names without a date (iPhone
`IMG_4865.JPG`, UUID names such as `…_4_5005_c.jpeg`, Facebook downloads) stay as they are;
fix those in the app, or leave them marked with the amber "?".

## Google Takeout import

The four zips live in `photo-archive/takeout-zips/` (outside `originals/`). On the Mac, copy
them over with `caffeinate -d rsync -avh --partial --progress ~/Downloads/takeout-*.zip
aj9@192.168.1.40:/media/aj9/Juniper13/photo-archive/takeout-zips/`, then on the server check
every zip: `for z in takeout-*.zip; do echo "== $z"; unzip -tq "$z" | tail -1; done`.

```
# 1. unpack ALL parts into one staging tree (outside originals/; run in tmux; 30 to 60 min)
mkdir -p /media/aj9/Juniper13/photo-archive/takeout-unpacked
cd /media/aj9/Juniper13/photo-archive/takeout-unpacked
for z in ../takeout-zips/takeout-*.zip; do echo "== $z"; unzip -q -o "$z"; done

# 2. look before you move: counts, unmatched sidecars, album copies (report file lists them all)
cd ~/photo-curator && git pull
scripts/import_takeout.py

# 3. move into originals/google/ (a rename on the same disk; nothing deleted or overwritten)
scripts/import_takeout.py --apply

# 4. index it, then fingerprint
scripts/scan_archive.py
~/venvs/curator/bin/python scripts/find_similar.py
```

The full report is `photo-archive/takeout-import-report.txt`: media without a sidecar, JSON
matching no photo, other files left alone, album copies skipped, name clashes. What the dry
run shows is what `--apply` will do. After applying, staging holds only what was not
imported (skipped album copies, unused sidecars); delete it only once the archive is backed up.
Dates: a photo with no EXIF date takes its date from the sidecar (`Date from Google Photos`
in the app's date-source filter). Keep the Google account's photos until the archive has a
second copy; Google's trash keeps deleted items 60 days.

## Run the browse app

A read-only web page over the scan index: a thumbnail grid you can filter by year, type
and date source, a detail page with the full-size preview and (for Live Photos) the
video, and an exact-duplicates page. It never changes a photo; it only writes cached
thumbnails to `photo-archive/thumbs/`.

```
# one-off setup on junipernine2
sudo apt install -y ffmpeg                          # video thumbnails
python3 -m venv ~/venvs/curator
~/venvs/curator/bin/pip install -r ~/photo-curator/requirements.txt

# run (inside tmux so it keeps running)
cd ~/photo-curator
~/venvs/curator/bin/uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Then open `http://192.168.1.40:8090` from the Mac. `--host 0.0.0.0` makes it reachable
by anything on your home network and **there is no login yet**; use `--host 127.0.0.1`
to keep it to the server itself. Cloudflare Access goes in front before it is exposed
outside the house.

Settings (environment variables, all optional): `ARCHIVE` (originals folder), `DB`
(index file), `THUMBS` (cache folder), `EDITS` (your manual corrections). Defaults match the
layout above.

### Fixing dates by hand

Open a photo and use **Fix the date**. "Set for this photo" changes that photo (and the
video of a Live Photo); "Set for the folder" changes the files directly inside the same
folder, by default only the ones dated from file date. Corrections are saved in
`photo-archive/curator_edits.sqlite` (table `date_override`, with the previous date kept),
not in the photos and not in the scan index, so re-scanning never undoes them. Filter with
"Date set by me". "Undo my date for this photo" removes a correction. Back up
`curator_edits.sqlite` along with `curator.sqlite`: it is the only copy of your decisions.

Notes:

- Thumbnails are made the first time you look at them and cached, so the first scroll
  through a year is slower than the second. RAW (CR2) files use the preview image
  embedded in the file (needs `exiftool`); videos use one frame via `ffmpeg`.
- If the scan has not been run, the page says so (HTTP 503).
- Re-run `scripts/scan_archive.py` after adding photos; the page reads the new index
  on the next load.
- htmx is vendored in `app/static/` so the page works with no internet.

## Find near-duplicates

`scripts/find_similar.py` finds photos that look alike but are not byte-identical: resized
or re-saved copies, lightly edited versions, a RAW and its JPEG, and burst shots. It is
read-only on the photos. It makes a 64-bit "fingerprint" of each photo's thumbnail (so it
also creates every thumbnail, which is slow the first time) and groups close fingerprints.

```
cd ~/photo-curator && git pull
~/venvs/curator/bin/python scripts/find_similar.py --limit 300     # small trial first
~/venvs/curator/bin/python scripts/find_similar.py                 # then everything (resumable)
```

Run it in tmux for the full archive. Then open the **Similar** page in the browse app.
Groups are listed with the most space-wasting first. Each group shows how far apart the
photos were taken (seconds apart means a burst), and marks its **Sharpest** frame (best for
bursts) and its **Largest** version (best for copies). Both are suggestions only. A photo's own page also lists its similar photos. The page can be filtered to
**same photo in several sources** (an iCloud original and an old export, say), **bursts** (one
source, within 10 seconds), **spread over time** or **camera time unknown**, sorted by spare space or by number of photos, and a group can be opened by its
number (`/similar?group=192`).

- Results are in `photo-archive/curator_similar.sqlite`. It is derived data: delete it and
  re-run to rebuild. Re-runs only fingerprint new or changed photos.
- `--threshold N` (0 to 7, default 5) is how many of the 64 bits may differ. Lower is
  stricter. Bursts of the dogs will group at 5; use 2 or 3 to see only near-identical copies.
- Misses are expected for heavy crops and rotated copies. Blank or very dark pictures are
  skipped (they would all match each other). Byte-identical files are left to the Duplicates page.
- The sharpness score is a simple edge measure on the thumbnail. It separates a blurred frame
  from a crisp one in the same burst. Don't compare scores across different scenes, and a
  heavily brightened or noisy copy can score high; for copies, trust Largest.
- Re-run it after each scan. Photos fingerprinted by an older version are re-scored automatically.
- A photo that cannot be read gets status `failed` and a `.fail` marker beside its thumbnail
  (so the browse app does not retry it on every page load). If you fix the cause, delete the
  markers (`find photo-archive/thumbs -name '*.fail' -delete`; they are cache only) and re-run
  `find_similar.py`, which retries failed photos. Photos missing a few bytes at the end (9 Oct:
  92 Pixel burst covers, 37 bytes short) are now read anyway.

## Bringing in an old photo folder, and retiring it

Old backup folders found on other disks (for example `/media/aj9/Juniper13/Pictures`, 53GB)
go into the **Old Photo Folders** bucket, `originals/photos-catchall/<name>/`.

```
tmux new -s pictures
rsync -avh --partial --progress --exclude '*.photoslibrary' --exclude '.DS_Store' --exclude 'Thumbs.db' \
  "$SRC"/ "$DST"/        # DST=.../originals/photos-catchall/<name>
```

Run it once with `-n` first to see the size. Then rescan (`scripts/scan_archive.py`), run
`find_similar.py`, and compare the exact-duplicate group count on the Duplicates page before
and after: a big jump means the folder was mostly repeats.

**Old `.photoslibrary` packages** are not copied whole. Look inside first:
`du -sh <pkg>/*`, then count files in `originals/` (and `Masters/`, `Modified/` for iPhoto
libraries). Copy only `originals/` into its own folder, e.g. `iPhoto-Library-originals/`.
Skip `resources/` (previews and thumbnails), `database/` and `Data/`. File names inside are
UUIDs; dates still come from EXIF. Albums, captions, faces and edits stay in the package
databases and are not carried across. Compare two similar-looking packages first, with a
path-and-size listing diff, and copy only one if they match:

```
diff <(cd "A.photoslibrary/originals" && find . -type f -printf '%P %s\n' | sort) \
     <(cd "B.photoslibrary/originals" && find . -type f -printf '%P %s\n' | sort) && echo IDENTICAL
```

Record from the 9 Oct import of `Pictures`: `iPhoto Library` and `iPhoto Library 3` were
identical (1,197 originals, 653 JPEG and 544 CR2, 14G each); `Photos Library.photoslibrary`
(868 originals) was a separate, smaller library, not a copy of the parked one.

### Before deleting the source folder

Never delete a source folder until all of these are done, in this order:

1. **Prove the copy.** Re-run the rsync as a checksum dry run (`-avhnc`, same excludes). It
   must list no files. For packages, run the same check on the `originals/` folders.
2. **Second copy of the archive.** The old folder and `photo-archive` may be on the same
   disk (`Juniper13` holds both). Deleting the old folder then leaves one copy on one
   drive. Back up `photo-archive` (including `curator_edits.sqlite`) to another disk, and
   test a restore of a few files, first.
3. **Decide about the packages.** If albums or captions might matter, move the whole
   `.photoslibrary` packages (about 35GB for `Pictures`) into `originals/_apple-libraries-raw/`
   (parked, never indexed) rather than deleting them.
4. **Then** delete the remainder, with Tony's explicit go-ahead. Claude never deletes
   photos itself.

## Git and GitHub

- Claude commits as `Claude <noreply@anthropic.com>`. The repo is public; never commit photos, `.env`, the cookie folder or tokens (the `.gitignore` covers photos, databases and `.env`).
- For Claude to push, the Claude GitHub App needs **read and write** on this repo (GitHub > Settings > Applications). Reading alone lets Claude see the repo but pushes are refused.
- On the server, pull updates with `cd ~/photo-curator && git pull`.
- One-off hand-off scripts go in `adhoc_scripts/` (git-ignored).

## Don't

- Don't use `--auto-delete` or `--keep-icloud-recent-days`; they delete files.
- Don't pass the Apple password with `--password` on the command line (it lands in shell history).
- Don't paste full debug output from icloudpd into chats or issues. It can include session tokens; the last 10 to 15 lines are enough.
- Don't delete anything from iCloud or Google until the archive has been verified.

## Open items

- Finish the first trial download and confirm Live Photo pairing and folder layout.
- Run the full Apple pull and verify counts.
- Run the scan on the archive and review the report (dates, duplicates, Live Photo pairs).
- Google Takeout: unpack, dry-run and apply `import_takeout.py`, scan, similar pass (see "Google Takeout import").
- Finish the Mac download, export, rsync and scan; check the Live pairs and counts against Photos.
- Run `find_similar.py` over the whole archive and review the Similar page.
- Finish the `Pictures` import (main folder plus the two package `originals/` folders), rescan,
  run `find_similar.py`, then retire the source folder using the checklist above.
- Build keep/reject marks and the review page.
- Install Immich and run a trial import.

## Parking generated thumbnails (`park_thumbnails.py`)

Some old exports contain tool-made small copies named like `J240x240-09561.jpg`
(`PicturesPreFreya`: about 12,500 of 21,251 files). They have no camera dates, so they all
land on the export date and make "similar" groups with their own originals. The script
moves the clearly small ones (both sides at most 500 px, no camera make) into
`originals/_thumbnails-<name>/`, which the scanner skips. It never deletes; a manifest
allows `--undo`. Larger `J…` files and any with camera data stay indexed for review.

```
git pull
scripts/park_thumbnails.py            # dry run: counts and examples
scripts/park_thumbnails.py --apply
scripts/scan_archive.py               # forgets the moved files
~/venvs/curator/bin/python scripts/find_similar.py
```

The backup rsync has no `--delete`, so the backup keeps the old paths as well as the new
`_thumbnails-…` ones until you choose to tidy it (about 0.7 GB).

## Timeline (year by month)

`/timeline` shows a year-by-month grid of how many items there are; empty months are outlined
as gaps, and each month links to its photos. "Leave out guessed dates" ignores files dated only
by their file date, so a bulk-copy pile-up cannot hide a real gap. The Photos page also has a
Month filter next to Year. Read-only: it reads the same index as the other pages.

### Apple preview files (`<UUID>_4_5005_c.jpeg`)

Photos leaves small preview copies of photos (2,964 here, dated only by the day they were copied,
which piled up in March 2021). `scripts/park_thumbnails.py --previews` parks a preview into
`originals/_previews-apple/` only when the archive holds a full-size version: a file with the same UUID
in its name, or a member of the same similar-photo group with at least 2x the pixels (`--min-ratio`).
Previews with no twin stay indexed. Dry run by default; `--apply` moves, `--undo` puts back (use the
same `--previews`). Then run `scan_archive.py` and `find_similar.py`.
