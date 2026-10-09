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

`ffmpeg`, OpenCV, `pillow-heif`, `imagehash` (frame picker, thumbnails, near-duplicates);
FastAPI, Uvicorn, Jinja2 and HTMX (browse page); Immich with PostgreSQL + pgvector;
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
came from (`exif`, then the `folder` name like `2013_02_10`, then file `mtime`),
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
  google/                        Takeout zips (requested, waiting)
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
- Google Takeout requested 9 Oct 11:11; download all parts when Google emails.
- Install Immich and run a trial import.
