# Ingest: Apple Photos

Goal: copy every original out of iCloud Photos onto junipernine2. **Copy only** —
nothing is deleted from iCloud or the server until the archive has been checked.

## Layout

```
/media/aj9/Juniper13/photo-archive/
  originals/apple/     # icloudpd output (year/month folders), never edited
  originals/google/    # Takeout zips, untouched
  originals/scans/     # scanned prints
```

Photo data lives outside this repo. The repo only holds code and docs.

## Prerequisites

- Advanced Data Protection **off** (otherwise Apple returns access denied).
- iCloud **Access iCloud Data on the Web** turned on (iPhone: Settings > your name > iCloud).
- Free space: library is roughly 30GB; Juniper13 has 2TB+.

## Install icloudpd on junipernine2

Check the project's release notes for the current method; the simplest is a venv:

```
python3 -m venv ~/venvs/icloudpd
~/venvs/icloudpd/bin/pip install icloudpd
export PATH=~/venvs/icloudpd/bin:$PATH
icloudpd --help
```

## Steps

```
export ICLOUD_USER='you@example.com'
scripts/apple_pull.sh auth      # prompts for password + 2FA code, once
scripts/apple_pull.sh trial     # 20 most recent photos
```

Check the trial before the full pull:

- a Live Photo arrives as a `.HEIC` plus a `.MOV` with matching names
- dates and folders look right
- files open (HEIC needs a viewer that supports it)

Then, inside tmux so a dropped SSH session doesn't stop it:

```
tmux new -s applepull
scripts/apple_pull.sh full
```

The run is resumable: re-running skips files already downloaded.

Note: the server has no desktop keyring, so the script uses
`--password-provider console --mfa-provider console` and prompts at the terminal.

## Verify before trusting it

- Compare the file count on disk with Photos' count on the iPhone (Albums > Media Types shows photos, videos, Live Photos).
- Spot-check a few from each year.

## Known limits

- icloudpd gives files and dates. As far as we know it does not carry albums or
  favourites across. Revisit later with `osxphotos` if those matter.
- Never use `--auto-delete`.
