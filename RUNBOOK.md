# Runbook

Commands, settings and fixes for running photo-curator. See
[ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together.

## Dependencies on junipernine2

| Needed | Why | Notes |
|---|---|---|
| `git` | Pull this repo | Same credentials as recipe-app |
| Python 3 + `python3-venv` | Run icloudpd in an isolated environment | `sudo apt install -y python3-venv` if `venv` fails |
| `icloudpd` (PyPI package, in a venv) | Download from iCloud Photos | Installed in `~/venvs/icloudpd`; a standalone executable is also published on the project's GitHub releases page |
| `tmux` | Keep the long download running if SSH drops | |
| Free disk | Library is roughly 30GB | Juniper13 has 2TB+ free |

Later phases will add `ffmpeg`, OpenCV, `pillow-heif`, `imagehash`, Immich,
PostgreSQL and Ollama. These are not needed yet.

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
export ICLOUD_USER='you@example.com'         # your Apple ID email
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
| `Apple iCloud Photo Library has not finished indexing yet` | Apple builds a web-access index of the library the first time something other than the Photos app asks for it; with a library of this size it takes a while | Wait and retry (see below) |

### About the indexing wait

This is our understanding rather than something Apple documents precisely:
iCloud Photos has to prepare the library for web access before a client like
`icloudpd` can list it. Until that finishes, the tool can log in but sees
nothing to download. The time is up to Apple (anywhere from minutes to hours).

What to do:

- Open https://www.icloud.com/photos in a browser and leave it open; it signals when the library is ready, because your photos will load there.
- Wait 15 to 30 minutes between retries rather than re-running repeatedly.
- Don't change things in Photos on the iPhone in the meantime.
- Success looks like `Downloading ... IMG_xxxx.HEIC` lines instead of the indexing message.

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
- Request the Google Takeout (all parts, Photos only).
- Install Immich and run a trial import.
