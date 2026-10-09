Inspired by my recipe app in the JuniperNine Recipe App Project I want to create my own offline storage, curation and presentation solution for my photos from Apple Photos, Google Photos and various offline real photos from over the years. I am constantly taking pictures with my iPhone that are 'Live photos' and this blows my storage. The Google photos are also in need of a clean up - I'd like to create a mechanism for storing the photos - much like my recipe app, viewing them - tidying them and presenting them. One thing I want to consider before I start is whether there are libraries for resolving a Live Photo from Apple the way you do in the apple interface or whether i'd have to do this myself first. My intention is to host this on the same server as my recipe app - I have LOTS of space - I also intend to use something like Qwen to do some clever tagging or resolution of poor or duplicate images. Is this viable - and can you suggest a starting point - would it be similar to my recipe-app presentation?

Yes, it’s viable, and your setup is well suited to it: plenty of disk, a 12GB GPU already running Qwen, and the Cloudflare Tunnel + Access pattern you already use for recipes. Before you start building, though, here’s a suggestion that might save you months.

Live Photos: you don’t have to do this yourself

A Live Photo isn’t a special format. It’s two files, a HEIC still and a roughly 3-second MOV, linked by a shared identifier in their metadata. To “resolve” one the way the Apple interface does, you keep the HEIC (or the edited key frame, if you changed it) and decide what to do with the MOV. Several existing tools handle this:

osxphotos (Python, runs on a Mac) reads the Photos library directly. It can export stills only, skipping the Live video, and it understands edits, albums, favourites and people. This is the best way to export from Apple with the curation you’ve already done intact.
icloudpd runs on Linux, so it could live on junipernine2. It pulls straight from iCloud and has options to skip the Live video or only keep it.
exiftool pairs HEIC and MOV files by their content identifier, if you end up with loose files.
For Google Takeout, which scatters metadata into JSON sidecar files, immich-go uses Google Photos’ date and location instead of EXIF data and handles recognising iPhone Live photos even when the filename is duplicated, e.g. IMG_2710(1).MP4 and IMG_2710(1).HEIC. 
github
github

To stop the storage problem at the source, you can turn Live off by default on the iPhone. The setting to make it stick is under Settings → Camera → Preserve Settings.

Consider Immich as the engine, not something you build

Immich is a self-hosted Google Photos replacement, and it already covers much of what you’ve described:

ingests photos from iPhone, Google Takeout and folders of scans
pairs Live Photos automatically
does face recognition and CLIP “smart search” (e.g. “dog on a beach”) on the GPU
has a duplicate review screen
has iOS and Android apps for ongoing backup

Rebuilding the hard parts yourself (HEIC decoding, thumbnails for 100k+ images, video, EXIF, maps) would be a big job before you even reach the interesting curation work.

Where your own app fits

I’d use a hybrid, which keeps the part you enjoy, writing your own FastAPI + SQLite + HTMX code:

Immich is the store and viewer. Everything gets ingested there, and it’s the day-to-day browsing app for you and your wife.
A “photo curator” app is yours. It looks like recipe-app, talks to Immich’s API and does the clever work:
finds near-duplicates with perceptual hashing (the Python imagehash library) and bursts by time window
scores blur and exposure (OpenCV’s Laplacian variance is a simple blur check)
asks a Qwen vision model through Ollama (qwen2.5vl or qwen3-vl at 7–8B fits in 12GB) things like “which of these 5 is the best shot and why” or “caption and tag this scanned 1980s print”
writes the results back to Immich as tags, albums or stacks
Review, never auto-delete. This is the same rule you settled on for media_inspector. The curator proposes “keep this one, bin these four”, and you approve each group, much like your one-book-at-a-time review for recipes.

So yes, the presentation would feel like recipe-app: a server-rendered HTMX review queue behind Cloudflare Access at something like photos.junipernine.com. The difference is that the heavy lifting of storage and viewing comes for free.

Practical cautions

Takeout is messy. You need every part of the export, or the metadata goes missing. Photos with missing JSON files are a common problem, and the fix is to request another takeout. Takeouts also contain lots of duplicates, often the same photo at full resolution and in Google’s compressed version. 
github
GPU sharing. Immich’s machine learning and Ollama will compete for the 4070’s 12GB. Run big Qwen batches when Immich isn’t doing its first indexing pass.
Clean-up happens at the source. Removing Live videos from iCloud itself means switching Live off on each photo in the Photos app (or deleting the photo once it’s safely archived). Your server copy doesn’t free up iCloud space on its own.

A starting point

Install Immich (Docker Compose is its standard route, even though you went native for Ollama). Import a small test batch: one Takeout part and a few hundred iPhone photos via osxphotos or icloudpd. Check that Live Photo pairing and dates come out right.
Once you trust the import, do the full ingest.
Then start the curator repo, with duplicate detection and a review page as the first feature, and add Qwen tagging after that.

Want me to sketch the curator’s data model and the first duplicate-detection script, the same way we built recipe-app?

Sources:

immich-go releases
immich-go issue #390

So I don't want to turn off Live Photo - it is very helpful for my fast moving black dogs - or my every fidgetting wife - so I do like to find the best picture from the 3 second Mov.  I was hoping there was an offline way to manipulate the HEIC in browser.  Is there a way to go native with Immich - I don't like how resource hungry Docker is on my mac.

Good news on both, with one caveat about the frames.

Picking the best frame from the Live video

This is very doable, and a good fit for your own app. Immich will store and play Live Photos, but as far as I know it has no “choose key photo” feature, so this is where your curator earns its keep.

The caveat: the MOV is much lower resolution than the still. On recent iPhones the video is around 1920×1440, against a 12–48MP HEIC. Apple’s own “Make Key Photo” has the same limitation; it’s just less obvious on a phone screen. So a frame from the video is a rescue for when the still caught the dog mid-blur. It won’t beat a sharp still. It’ll look fine on screen or as a 6×4 print, less so cropped or printed large.

A flow that fits your FastAPI + HTMX stack:

Extract frames on the server. ffmpeg pulls all ~90 frames (3 seconds at 30fps) from the MOV into a temp folder.
Auto-shortlist. Score each frame for sharpness (OpenCV’s Laplacian variance), keep the top 5–6, and optionally ask Qwen-VL which has eyes open, the dog’s face visible, and so on.
Scrubber page. Show the original still beside a strip of candidate frames. You click one, and HTMX posts the choice back.
Save, don’t replace. Write the chosen frame as a new JPEG next to the original, keeping the HEIC and MOV untouched, and push it into Immich stacked with the original.

HEIC in the browser

Safari displays HEIC natively; Chrome and Firefox mostly don’t. The simple fix is to have the server generate JPEG/WebP previews with pillow-heif, which is also what Immich does internally. Everything stays on your LAN, so it’s all offline in the sense that matters; nothing goes to the cloud. If you ever want decoding done purely in the browser, libheif-js (a WebAssembly build) can do it, but server-side previews are simpler and faster.

The Live videos themselves (HEVC) play in Safari. For other browsers, extracting frames with ffmpeg, as above, sidesteps the problem.

Immich without Docker

First, I think the resource hunger you’ve seen on the Mac comes from Docker Desktop, which runs a whole Linux virtual machine. On junipernine2, which runs Linux, containers run directly on the host kernel with very little overhead. So Docker there may not be the burden you’re picturing. The Mac only needs to run osxphotos for the export.

If you’d still rather go native, which fits how you moved Ollama off Docker:

It’s unofficial. Docker is still the only officially supported install method, and the issue asking for native support has been open for years.
arter97/immich-native is the established community route. It provides helper scripts to install Immich without Docker, installing to /var/lib/immich and running as systemd services.
It uses your own Postgres with pgvector, plus Redis and Jellyfin’s ffmpeg build. As an ex-DBA you’d probably find that more comfortable than a database hidden inside a container.
Upgrades are the cost. Immich releases often, and with a native install you rebuild on each upgrade and hope the scripts keep up. Some people settle on Podman as a middle ground: no Docker daemon, same compose files, official upgrade path.

My suggestion: try it in Docker on junipernine2 first for the test import, and watch memory and CPU there. If it still bothers you, moving to the native install later is straightforward, because your photos live on disk and the database can be dumped and restored.

Want me to start with the frame-extraction and sharpness-scoring script? It works on its own, before Immich is even installed, so you could try it on a few dog photos this weekend.

Sources:

arter97/immich-native
Immich discussion #1657: native deployments
Immich in LXC guide
Lemmy thread on Immich without containers

sounds like I really need to turn off live photo unless I'm photographing the dogs - I'm compromising the quality over convenience.

Not quite: leaving Live Photo on doesn’t cost you any quality. The iPhone still takes the full-resolution HEIC exactly as it would with Live off; the 3-second video is captured alongside it as an extra. The only cost is storage, roughly doubling each photo’s size.

The quality trade-off only happens when you choose a frame from the video instead of the still. So with Live on, you always have the full-quality still, plus a lower-quality backup if the still caught the dog mid-blur.

That means your choice is really about storage, and you have a few options:

Leave it on and let the server take the strain. Once photos are archived on junipernine2, which has the space, you can remove them from the phone and iCloud. Your curator could also drop the MOV from anything that isn’t a keeper, or where you’ve accepted the still.
Toggle it per situation. Live on for the dogs and your wife, off for landscapes, food and documents. Turn on Settings → Camera → Preserve Settings → Live Photo so the camera remembers your last choice rather than resetting to on.
Use burst mode for the fastest moments. Hold the shutter and drag left, or set volume-up to burst in Camera settings. Burst gives you full-resolution stills at around 10 per second, rather than lower-resolution video frames, so it’s the better tool when the dogs are really moving.
Given your fast black dogs, I’d keep Live on as your default and let the archive handle the storage, which is the problem you’re solving anyway.

I have returned and would like to wake this project up and start putting it together - lets get a github repository created - and start with the spec we talked about last time.

I’ll draft the spec into a fresh local repo now; the GitHub sign-in available here isn’t working, so I can’t create the repo on GitHub myself.

The spec is written up as SPEC.md, with a README and a .gitignore that keeps photos, frames, databases and adhoc_scripts/ out of the repo. All three are committed locally as the first commit.

It captures what we settled on: Immich as the store and viewer, your own FastAPI + SQLite + HTMX curator alongside it, the Live frame picker as the first feature, never auto-deleting, and Qwen-VL running locally. It also includes a data model, the screens, eight phases, and the open questions still to decide.

One step needs you: the GitHub sign-in available to me here isn’t working, so I can’t create the repo myself. On GitHub, create an empty repo under junipernineaj (no README or licence, so it doesn’t clash with my first commit). I’ve called it photo-curator, but name it whatever you like and tell me. Public or private is up to you; no photos can end up in it either way. Once it exists, I’ll attach it and push.

Then Phase 0’s last item is the standalone frame-picker script, which you could try on a few dog photos before Immich is even installed.

I want - like the recipe-app - to have the repo on my plex server and to operate the way we did last time with you having oversight of the repo in a public state - with an authorised token - shall we do this?

Searched past chats
Yes, let’s do it the same way. Here’s how it would work:

You create the repo on GitHub: junipernineaj/photo-curator, public, and empty (no README, licence or .gitignore, so it doesn’t clash with the first commit I’ve already made).
I attach it here and push. My access comes through the GitHub app authorisation for your account, so you don’t need to give me a personal token. Please don’t paste one into the chat. If the app isn’t allowed to reach the new repo, the attach step will say so and point to the setting to change.
You clone it on junipernine2 using the same Git credentials you use for recipe-app, e.g. git clone git@github.com:junipernineaj/photo-curator.git in the folder next to recipe-app.
From then on it’s the recipe-app routine: I read and commit to the repo, you git pull on the server and run things there. One-off hand-off scripts go in the git-ignored adhoc_scripts/ folder via scp, so they never land in the public repo.

Since it’s public, the .gitignore already keeps out photos, frames, databases and .env files. The Immich API key will live in .env on the server only.

Let me know once the empty repo exists and I’ll push the spec.

we have a repo - https://github.com/junipernineaj/photo-curator

Add the repository Failed
I can’t attach the repo yet. The attach step failed with “link your GitHub account to let Claude access repositories”, so Claude isn’t connected to your GitHub account.

To fix it, open claude.ai Settings → Connectors, connect GitHub, and approve access for junipernineaj/photo-curator. If you installed the GitHub app on specific repos only, add the new one there too. As before, don’t paste a personal access token into the chat. Once it’s linked, tell me and I’ll retry the attach and push the spec.

If you’d rather not wait, I can package the first commit as a single git bundle file. You could then clone from it on junipernine2 and push it to the empty repo yourself. That would still leave me without oversight of the repo until the account is linked.

interesting - in another project - my recipe-app chat - you are connected to my github

List your repositories junipernine Failed
I checked again and this session has no GitHub account linked, so it can’t see any of your repos, recipe-app included.

I can’t see how the recipe-app chat is set up, so I don’t know why it works there. My guess is that it’s a different kind of session, with its own GitHub connection. Linking in claude.ai Settings → Connectors may be all this one needs. If you’ve already connected GitHub there, it may only need to be allowed for this session, or for the new repo.

You have two options:

Link GitHub for this session via Settings → Connectors, then tell me and I’ll retry the attach and push.
Move the repo work to the recipe-app chat, since it already works. I can package the first commit as a git bundle (spec, README, .gitignore) for you to take there, or you can paste the SPEC.md I sent into that chat.
Option 1 keeps everything in this conversation, so I’d try it first. If it doesn’t work after a couple of minutes, go with option 2. Which do you want?

when I try to connect to github from claude it takes me through a whole onboarding setup as if I've never used claude - that can't be right surely?

It’s plausible, and I don’t think it means anything is wrong with your account. I can’t see the screens you’re getting, so this is my best guess.

GitHub access for coding sessions like this one is set up separately from the GitHub connector in regular chat. The recipe-app chat may be using the connector, while this session needs the “Claude Code” GitHub app. The first time you use it, it runs its own first-time setup, which looks like onboarding for a new user. That would explain why it feels like you’ve never used Claude.

One thing to check is whether you’re signed in to the same account in both places. A different login would also produce a fresh onboarding.

If the flow is only asking you to install the GitHub app and choose repositories, it’s fine to go through it. Limit access to junipernineaj/photo-curator, or to the repos you want me to see. If it’s asking for payment or plan details, or looks like a setup for something unrelated, stop there.

If it turns into a rabbit hole, the quick route is to take the work to the recipe-app chat where GitHub already works. Say the word and I’ll package the first commit as a git bundle, with the spec, README and .gitignore, that you can clone from and push to the repo yourself.

That screenshot shows you’re signed in as Tony on the Pro plan, with the AJ avatar. It doesn’t tell me which screen you took it from, and I can’t see the recipe-app chat, so I can’t compare the two myself.

To check, open the recipe-app chat and look at the same bottom-left account menu. If it also says Tony · Pro with the AJ avatar, it’s the same account. To be certain, click the menu in each place and compare the email address it shows. If the addresses differ, that explains the fresh onboarding, and signing in with the account that has your GitHub link should fix it.

If both are the same account, the likelier explanation is the separate GitHub app setup I described. In that case the onboarding is probably harmless if you limit access to photo-curator.

Tell me what the email comparison shows and I’ll take it from there. Or if you’d rather skip all this, I’ll make the git bundle and you can push from junipernine2.

the screenshot is from within the claude app itself at the bottom of all my chats

Then it’s the same account everywhere, so a different login isn’t the cause. The account menu sits under all your chats.

So the difference is in how GitHub is attached. The recipe-app chat probably has its connection set up already, while this session uses a separate coding-session link that hasn’t been authorised yet. That’s why the prompt looks like first-time setup, even though you’re not new.

I’d go through the flow once. If it’s a GitHub app install, choose Only select repositories and tick photo-curator (add recipe-app too if you want me to see it). Skip anything unrelated, like plan or payment screens. Then tell me and I’ll retry the attach.

If it still treats you as new, or the link doesn’t take, I’ll make the git bundle so you can push from junipernine2 and carry on.

I'm not using the github app - I am using github in a browser

That’s fine, and I wasn’t clear. By “GitHub app” I didn’t mean a program on your computer. I meant the authorisation page GitHub shows in the browser, along the lines of “Authorize Claude” or “Install Claude on your account”. Using GitHub in a browser is exactly how that works.

Where the onboarding sits is my guess. If the first-time setup appears on Claude’s side, before you ever reach GitHub’s page, I can’t tell from here why.

What’s the first thing the screen says or asks you to do? A short description, or a screenshot, is enough for me to say whether it’s the normal link flow or something to skip.

Or, to keep things moving, I’ll make the git bundle now. You’d clone it on junipernine2 and push to your empty photo-curator repo. Linking GitHub to this session can wait until later.

this pops up-  you already know who I am so I find this odd

Examining the structure of a Claude onboarding URL.

That explains it, and it isn’t a sign that something’s wrong with your account. The address bar says open_in_browser=1&auth_start=github, so the app handed the GitHub link off to this Chrome window, and Chrome is asking for a name because it doesn’t recognise you. Most likely this browser is signed in to a different Claude account, or not signed in at all. The Claude app and Chrome keep separate logins, so the app knows you as Tony and Chrome may not.

Please don’t type a name and continue. That could finish setting up the wrong account and attach GitHub to it instead of yours.

Instead:

Open a new tab in that Chrome window and go to claude.ai.
Check which account it shows. If it’s logged out or a different account, sign in with the same email as the app.
Go back to the app and start the GitHub connect again, so it opens in the now signed-in browser.

If Chrome’s profile is signed in as Tony already and still shows onboarding, tell me and we’ll stop there. In that case I’ll make the git bundle so you can push from junipernine2 and skip this link entirely.

ah I see - lets try that - ok you were right - wrong acct in the browser - lets try again - can you see my github repo

Yes, I can see junipernineaj/photo-curator now. But I can’t push to it yet, and GitHub is refusing writes from this session.

The repo wasn’t quite empty. GitHub had already added a one-line README and a .gitignore containing ./adhoc_scripts. I rebased my commit on top of that, so the history is add .gitignore file followed by my Initial spec, README and .gitignore. My fuller README and .gitignore replace the one-liners, and adhoc_scripts/ is still ignored. It’s all ready to push locally.

The push is blocked because the attach linked your account for reading, but the Claude GitHub App doesn’t have write permission on this repo. To fix it:

Open https://github.com/apps/claude/installations/select_target and install it on your junipernineaj account.
Select Only select repositories and tick photo-curator, plus recipe-app if you want me on that too.
If it already shows as installed, open its settings in GitHub, check that photo-curator is in the list, and make sure contents access is read and write.

Tell me when that’s done and I’ll push straight away. If you’d rather not touch it, I’ll make the git bundle and you can push from junipernine2 instead.

My commit is now authored by Claude (noreply@anthropic.com), as the check wanted. Your original GitHub commit, add .gitignore file, is unchanged.

The push is still blocked. The Claude GitHub App needs read and write access to photo-curator. Install it at https://github.com/apps/claude/installations/select_target, choose Only select repositories, and tick photo-curator. If it’s already installed, check that the repo is in its list in GitHub’s settings.

Tell me when that’s done and I’ll push the spec. If you’d rather not, I can make the git bundle for you to push from junipernine2.

ok lets try again

The push worked. SPEC.md, the README and the .gitignore are now on main at https://github.com/junipernineaj/photo-curator, on top of your add .gitignore file commit.

To get it onto the server, run this on junipernine2, next to recipe-app:

git clone https://github.com/junipernineaj/photo-curator.git

Use the SSH URL instead if that’s how recipe-app is set up.

For the next step, the Phase 0 frame-picker script is a good place to start. I’d have it take a Live Photo’s MOV, extract the frames with ffmpeg, score them for sharpness and save the top few. You could then try it on a handful of dog photos before Immich is installed. Shall I write it?

we are in business then - I think the best starting point is to pull all the photos from Apple and Google

Planning photo export options across Apple and iCloud tools.

Agreed. Pulling everything in first is the safe order, because nothing downstream (Immich, the curator, Qwen) can start without it. One rule for this step: copy only, delete nothing from Apple or Google until the archive is verified.

Apple: one thing decides the route

If iCloud Photos has “Optimise Storage” turned on, your Mac only holds small previews of most photos, and the originals live in iCloud.

Mac Photos library holds the originals. Run osxphotos export on the Mac. It keeps albums, favourites, edits and HEIC+MOV pairs.
Originals are only in iCloud. Use icloudpd on junipernine2 to pull them straight from iCloud. It’s a good fit for the server and doesn’t need the Mac to download everything first. Apple’s Advanced Data Protection, if you’ve turned it on, blocks this route. You’d need to export from the Mac instead.

Google: Takeout

Request Photos only, and tick everything including albums. Choose 50GB zip parts for fewer files.
Download every part into one folder on junipernine2 and leave them zipped. immich-go reads the zips directly, and keeping them lets you re-run the import if anything goes wrong.
Takeout can take hours or days to prepare, so request it now and let it cook while we set up the rest.

Landing zone on the server

/data/photos-staging/
  apple/        # osxphotos or icloudpd output
  google/       # takeout-*.zip, untouched
  scans/        # prints later

Immich ingests from here later, and the staging copies stay as your original set, much like your “original set” backup for the cookbooks.

A few things I need from you:

Is “Optimise iPhone/Mac Storage” on, and is the iPhone library synced to the Mac mini, or is it iCloud-only?
Roughly how big are the Apple and Google libraries? The iCloud and Google storage pages will tell you.
Which path should the staging folder go under on junipernine2? Which drive has the space?

With those answers I’ll write a short docs/INGEST.md and a small helper script in the repo for the route you pick.

lets start with Apple first - the photos are in the cloud as per this image - and it seems like there are 45 gig of images - so not that many - I plan on storing these in the same place as all the cookbooks - /media/aj9/Juniper13 but in a folder called photo-curator-local or something you might think more smart

Searched the web, read a page
Apple first is a good order. Before the commands, a correction on size: 43.85GB is your total iCloud usage. On the bar, Photos and Videos is roughly 60%, so probably around 30GB. That’s small, so the whole library can be pulled in one go.

Route: icloudpd on junipernine2. The originals are only in iCloud, so pulling them straight to the server avoids making the Mac download everything first. It saves each Live Photo as a separate HEIC and MOV, which is what we want. Its project page lists these requirements:

Advanced Data Protection must be off, or Apple returns an access-denied error. Check on your iPhone under Settings → your name → iCloud → Advanced Data Protection.
“Access iCloud Data on the Web” must be on.
Its first run is a 2FA login, which you do once with --auth-only before downloading anything.
Never use --auto-delete. It deletes local files that were removed from iCloud.
One trade-off: as far as I know, icloudpd gives you the files and dates but doesn’t carry over your albums or favourites. osxphotos on the Mac keeps those, but only for originals the Mac has actually downloaded. For a first pass I’d take the files with icloudpd and worry about albums later.

Folder: I’d keep the data separate from the code, and name it for what it is rather than the project:

/media/aj9/Juniper13/photo-archive/
  originals/apple/      # icloudpd output, never edited
  originals/google/     # takeout zips, untouched
  originals/scans/
Immich’s own library gets its own folder later, so originals/ stays a clean backup. Juniper13 is also your cookbook drive, so I’d check free space first.

Questions for you:

Is Advanced Data Protection on or off?
Can you run df -h /media/aj9/Juniper13 and paste what it shows?
If you answer those, I’ll add docs/INGEST.md and a small wrapper script to the repo. The script would do the 2FA check, then a trial run of the most recent 20 photos, then the full pull, inside tmux so it survives a dropped SSH session.

Source: icloud_photos_downloader on GitHub

Advanced data protection is off - and the space on Juniper13 is over 2 terrabytes - there's nothing to worry about there

Read a page, ran 3 commands
The ingest doc and wrapper script are pushed to the repo. I haven’t run any of it, because it needs your Apple login and 2FA on junipernine2.

While I was working, you added PROJECT_HISTORY.md to the repo, a tracker of this conversation. I rebased onto it and left it as it was. Its entry stops at my previous message and uses the /data/photos-staging layout I proposed then, which we’ve since replaced with /media/aj9/Juniper13/photo-archive/.

The ingest doc is docs/INGEST.md. The script, scripts/apple_pull.sh, has three steps (auth, trial and full). It writes into /media/aj9/Juniper13/photo-archive/originals/apple/ in year/month folders and has no delete options, and full is resumable. I wasn’t able to check some of icloudpd’s options against its documentation, so run icloudpd --help after installing and tell me if anything differs.

On junipernine2:

git pull in the repo, then install icloudpd in a venv (the doc has the three commands).
Run export ICLOUD_USER='your Apple ID email', then scripts/apple_pull.sh auth and enter your password and 2FA code.
Run scripts/apple_pull.sh trial, which downloads 20 photos. Check that a Live Photo arrives as a HEIC plus a MOV with matching names.
Paste me what the trial prints and lists. If it looks right, run full inside tmux.

ah icloudpd isnt installed - obviously

Right, I only wrote that step into the doc. On junipernine2:

sudo apt install -y python3-venv     # only if the next line complains
python3 -m venv ~/venvs/icloudpd
~/venvs/icloudpd/bin/pip install icloudpd
~/venvs/icloudpd/bin/icloudpd --help | head -30

The script looks for icloudpd on your PATH, so add the venv for the current shell session:

export PATH=~/venvs/icloudpd/bin:$PATH

To keep it across sessions, add that line to ~/.bashrc.

If pip can’t find a build that works for your Python version, the project also publishes a standalone executable on its GitHub releases page. Put it in ~/bin/ as icloudpd and chmod +x it.

Once icloudpd --help prints, carry on with git pull, then export ICLOUD_USER=... and scripts/apple_pull.sh auth. Paste me the first lines of --help as well. That lets me check that the options in the script (--directory, --cookie-directory, --folder-structure, --recent, --auth-only) exist in the version you got.

so - that didn't work - (venv) aj9@junipernine2:~/photo-curator/scripts$ ./apple_pull.sh auth
2026-10-09 09:35:06 INFO     Processing user: tenuoustoaster@gmail.com
2026-10-09 09:35:06 DEBUG    Authenticating...
Traceback (most recent call last):
  File "starters/icloudpd.py", line 6, in <module>
  File "icloudpd/cli.py", line 609, in cli
  File "icloudpd/base.py", line 261, in run_with_configs
  File "icloudpd/base.py", line 438, in _process_all_users_once
  File "icloudpd/base.py", line 902, in core_single_run
  File "icloudpd/authentication.py", line 86, in authenticator
  File "pyicloud_ipd/base.py", line 278, in __init__
  File "pyicloud_ipd/base.py", line 307, in authenticate
  File "icloudpd/authentication.py", line 80, in password_provider
  File "pyicloud_ipd/utils.py", line 46, in get_password_from_keyring
  File "keyring/core.py", line 63, in get_password
  File "keyring/backends/chainer.py", line 49, in get_password
  File "keyring/backends/SecretService.py", line 79, in get_password
  File "keyring/backends/SecretService.py", line 68, in get_preferred_collection
keyring.errors.KeyringLocked: Failed to unlock the collection!
[PYI-752402:ERROR] Failed to execute script 'icloudpd' due to unhandled exception!

Troubleshooting a headless server keyring issue during installation.

The install worked; this error is a different problem. icloudpd tried to fetch your Apple password from the system keyring (the desktop password store), and a headless server has none, so it fails with KeyringLocked. The fix is to make it ask you for the password at the terminal instead.

First, check that your version has the option for that:

icloudpd --help | grep -i -A3 password

If it lists a --password-provider option with console among the choices, run:

icloudpd --directory /media/aj9/Juniper13/photo-archive/originals/apple \
  --username "$ICLOUD_USER" --cookie-directory ~/.icloudpd-cookies \
  --password-provider console --auth-only

If there’s no such option, disable the keyring this way instead:

PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring ./apple_pull.sh auth

Either way it should prompt for your Apple password, then for the 2FA code on your devices. Please don’t use --password on the command line, because it ends up in your shell history.

Tell me which one works, or paste any new error. Then I’ll build it into apple_pull.sh and push, so trial and full don’t hit the same wall. After a successful auth, the session cookie is saved in ~/.icloudpd-cookies and you shouldn’t need the password again for a while.

(venv) aj9@junipernine2:~/photo-curator/scripts$ icloudpd --help | grep -i -A3 password
  --password-provider {console,keyring,parameter,webui}
                        Specify password providers to check in the given
                        order. Default: [parameter, keyring, console]
  --mfa-provider {console,webui}
                        Specify where to get the MFA code from
--
  --smtp-password SMTP_PASSWORD
                        SMTP password for sending email notifications when
                        two-step authentication expires.
  --smtp-host SMTP_HOST
                        SMTP server host for notifications
--
  -p, --password PASSWORD
                        iCloud password for the account if --password-
                        provider specifies parameter
(venv) aj9@junipernine2:~/photo-curator/scripts$

Your --help shows --password-provider console and --mfa-provider console, so I’ve added both to apple_pull.sh and pushed it. The script now asks for your password and 2FA code at the terminal instead of looking for a keyring.

On junipernine2:

cd ~/photo-curator && git pull
export ICLOUD_USER='tenuoustoaster@gmail.com'
scripts/apple_pull.sh auth

Enter your Apple password when asked, then the 6-digit code that appears on your iPhone or Mac. You should see it finish without a traceback.

If auth succeeds, run scripts/apple_pull.sh trial and paste me the output plus the result of:

find /media/aj9/Juniper13/photo-archive/originals/apple -type f | head -30

That will show whether Live Photos arrived as a HEIC plus a MOV with matching names.

that worked - 2026-10-09 09:36:50 DEBUG    Authenticating...
iCloud Password for tenuoustoaster@gmail.com:
2026-10-09 09:38:47 INFO     Two-factor authentication is required (2fa)
2026-10-09 09:38:48 DEBUG    2FA push notification triggered
  a: ***** ****54
Please enter two-factor authentication code or device index (a) to send SMS with a code: 415404
2026-10-09 09:38:59 INFO     Great, you're all set up. The script can now be run without user interaction until 2FA expires.
You can set up email notifications for when the two-factor authentication expires.
(Use --help to view information about SMTP options.)
2026-10-09 09:38:59 INFO     Authentication completed successfully

Authentication is done. The session is saved, so trial and full shouldn’t ask for your password again until Apple expires the 2FA, which can take a few weeks.

Next step is the trial run:

scripts/apple_pull.sh trial

That downloads your 20 most recent photos. Then paste me the output plus:

find /media/aj9/Juniper13/photo-archive/originals/apple -type f | head -30

What I’m checking for is that a Live Photo shows up as a .HEIC and a .MOV with the same base name, such as IMG_1234.HEIC and IMG_1234.MOV, in year/month folders. If that looks right, we do the full pull in tmux.

The login worked, but Apple stopped the download. The key lines are:

Apple iCloud setup is not complete. Please log into https://icloud.com/ to manually finish setting up your iCloud service
in the reply Apple sent back: "termsUpdateNeeded": true and "webservices": {}

So this isn’t a bug in the script. Apple has an updated iCloud terms notice waiting for you, and it won’t open the Photos service to anything until you accept it in a browser. The empty webservices means nothing was available to download.

To fix it:

In a browser, go to https://www.icloud.com and sign in as tenuoustoaster@gmail.com. Use the 2FA code, and if it asks “Trust this browser?”, choose Trust.
Accept any terms or setup screens that appear.
Click into Photos once and check that your library loads.
Make sure “Access iCloud Data on the Web” is still on, in iPhone Settings → your name → iCloud.
Re-run scripts/apple_pull.sh trial.

It will ask for your password again this time, which is fine, because the first attempt didn’t save a usable session. Re-run auth first only if the trial fails with 2FA again.

Please don’t paste the full debug output in future. This log included an unredacted trust token (the long HSARMTKNSRVX… string), which is a login credential. It’s only in this chat, so I wouldn’t worry, but for anything public, or if you want to be careful, the last 10–15 lines are all I need.

I’m taking the plainest route. I’ll restore the file exactly as I found it from my backup, then commit and push it with no edits.

PROJECT_HISTORY.md is committed and pushed to main as it stood, so the repo is clean again.

What’s in it: your 3 October entries, as I found them. They stop mid-sentence at the burst-mode advice, and I haven’t completed that.
What’s missing: everything from 9 October (the repo setup, the GitHub linking and wrong-account snag, the ingest scripts and the iCloud login) is not in the file yet.
Two attempts were stopped by the safety classifier, so I haven’t written anything further into it. If you want the 9 October part, the quickest route is probably to paste it in yourself from this conversation, or ask me for a short summary version and I’ll add that.

On the Apple pull: has scripts/apple_pull.sh trial got past the indexing message yet?

no not yet.  However - I do want the PROJECT_HISTORY.md summarise all the actiones we've performed up until now. And perhaps a new ARCHITECTURE.md files that explains how everything works - and a RUNBOOK.md that explains all the relevant commands and setup criteria needed (dependencies etc) - so we can maintain this going forward - I am particiulary interested in recording how we needed to hook up Apple Photos, what settings were needed - and why we need to wait for indexing etc.

All three are written and pushed to main:

PROJECT_HISTORY.md: I added a summary section at the end with what we decided, a timeline of today’s actions, the five snags we got past, and what’s still open. Your October 3 entries are as they were, including the cut-off sentence at the end of the burst-mode advice, which I haven’t completed.
ARCHITECTURE.md: how the pieces fit together, separating what exists now (the spec and the Apple ingest script) from what’s planned (Google, Immich, the curator, Qwen), plus where everything lives.
RUNBOOK.md: dependencies, the one-time setup and download commands, and the “don’t” list. It has a section on the Apple settings that must be right (Advanced Data Protection off, Access iCloud Data on the Web on, 2FA, terms accepted at icloud.com). Another section covers each snag we hit, with its symptom, cause and fix.

The indexing wait is described there as our understanding, not something Apple documents precisely. Apple seems to build a web-access index of the library the first time something like icloudpd asks for it. I don’t know how long it takes, so the runbook says minutes to hours.

Run git pull on junipernine2 to get the files. When you retry scripts/apple_pull.sh trial, send me the last 10–15 lines of what it prints.

still indexing - (venv) aj9@junipernine2:~/photo-curator$ scripts/apple_pull.sh trial
2026-10-09 09:58:01 INFO     Processing user: tenuoustoaster@gmail.com
2026-10-09 09:58:01 DEBUG    Authenticating...
2026-10-09 09:58:02 INFO     Apple iCloud Photo Library has not finished indexing yet
2026-10-09 09:58:02 DEBUG    {
  "request": {
    "method": "POST",
    "url": "https://setup.icloud.com/setup/ws/1/validate",
    "headers": {
      "Origin": "https://www.icloud.com",
      "Referer": "https://www.icloud.com/"
    },

That’s only 15 minutes since the first indexing message, so it’s still within the normal wait, and nothing you did is wrong. Authentication works, and Apple just isn’t ready to list the library yet.

While you wait, two checks are worth doing:

Does it load in the browser? Go to https://www.icloud.com/photos. If your photos appear there, the library is ready on Apple’s side, and the next trial should work. If it shows a spinner or says it’s still preparing, that confirms Apple is the bottleneck.
Is the iPhone syncing? In the Photos app, scroll to the bottom of the Library tab. A status like “Updating” or “Uploading” means iCloud is still busy. Plugging the iPhone in, on Wi-Fi, can help it finish.

Retry every 30 minutes or so instead of straight away. I don’t know how long this takes, and if it’s still not ready after a few hours, tell me and we’ll look at other causes.

If you’d rather not keep retrying by hand, I can add a wait mode to the script that checks every 20 minutes and starts the trial download when the library is ready. Shall I?

yes its fine in the browser

Working out why icloudpd reports incomplete indexing despite browser access.

Then I was wrong to suggest that the browser loading meant icloudpd would work. The site and icloudpd apparently check different things on Apple’s side, so the browser being fine doesn’t confirm anything about the tool. I don’t know why icloudpd still says “not finished indexing”.

The next step is to see whether your version has an option to deal with this. Please run:

icloudpd --help | grep -i -B1 -A3 -E 'index|skip|check'

Paste what it shows. I don’t want to guess at an option name, and I’d only suggest bypassing a check once we know what it does.

Meanwhile, the other thing I can do is the wait mode. It would retry every 20–30 minutes, and anything that does eventually work will then start without you watching the terminal. I’d rather see that help output first, though, in case there’s a better fix.

If nothing turns up, the fallback is osxphotos on the Mac. It needs the Mac to download the full-size originals from iCloud first, which takes a while for 30GB but doesn’t depend on this indexing step.

(venv) aj9@junipernine2:~/photo-curator$ icloudpd --help | grep -i -B1 -A3 -E 'index|skip|check'
  --password-provider {console,keyring,parameter,webui}
                        Specify password providers to check in the given
                        order. Default: [parameter, keyring, console]
  --mfa-provider {console,webui}
                        Specify where to get the MFA code from
--
  --list-libraries      List the available libraries
  --skip-videos         Don't download any videos (default: download all
                        photos and videos)
  --skip-live-photos    Don't download any live photos (default: download live
                        photos)
  --xmp-sidecar         Export additional data as XMP sidecar files (default:
                        don't export)
--
                        size-dedup-with-suffix
  --skip-created-before SKIP_CREATED_BEFORE
                        Do not process assets created before the specified
                        timestamp in ISO format (2025-01-02) or interval
                        backwards from now (20d = 20 days ago)
  --skip-created-after SKIP_CREATED_AFTER
                        Do not process assets created after the specified
                        timestamp in ISO format (2025-01-02) or interval
                        backwards from now (20d = 20 days ago)
  --skip-photos         Don't download any photos (default: download all
                        photos and videos)

USER options. Can be specified for setting user configuration only.

Checking for a library-listing option to find unindexed shares.

Your version has no option to skip or override the indexing check, so we can’t bypass it from our side. It’s Apple’s state, and we have to wait it out. I won’t suggest anything that pretends otherwise.

Two things in that output are useful:

--list-libraries shows which iCloud photo libraries your account has. Indexing is tracked per library. If you ever turned on an iCloud Shared Photo Library, the tool may be looking at a different one from the one that’s still indexing. It may hit the same indexing message, but it’s cheap to try, so run it once:
  icloudpd --directory /tmp/lib-check --username "$ICLOUD_USER" --cookie-directory ~/.icloudpd-cookies --password-provider console --list-libraries
--xmp-sidecar exports extra metadata into XMP sidecar files. I told you earlier that icloudpd probably doesn’t carry albums or favourites. This option may carry some of that metadata, so it goes on the to-check list for when downloads work.

Now I’m adding the wait mode so retrying doesn’t depend on you watching the terminal.

wait mode is pushed. I haven’t run it, because it needs your Apple session on the server.

On junipernine2:

cd ~/photo-curator && git pull
tmux new -s applewait
export ICLOUD_USER='you@example.com'     # your Apple ID email
scripts/apple_pull.sh wait

It runs the 20-photo trial every 20 minutes, up to 24 tries (about 8 hours), showing the last 15 lines each time. It stops when the “not finished indexing” message disappears and tells you to run full. Detach from tmux with Ctrl-b then d.

The --list-libraries check above is still worth running once, in case the tool is looking at a different library from the one that’s indexing. And if the trial is still stuck after a few hours, the fallback is osxphotos from the Mac.

I also added the wait mode and the “no skip option” note to RUNBOOK.md.

(venv) aj9@junipernine2:~/photo-curator$ scripts/apple_pull.sh wait
[10:02:57] attempt 1 of 24
      "pcsServiceIdentitiesIncluded": true,
      "userPartition": 163,
      "hsaChallengeRequired": false,
      "requestInfo": {
        "country": "GB",
        "timeZone": "GMT",
        "region": "EN"
      },
      "pcsDeleted": false,
      "iCloudInfo": {
        "SafariBookmarksHasMigratedToCloudKit": true
      }
    }
  }
}
An error (not just indexing) occurred - stopping. Read the output above.

That output ends with a normal login response, and it now says "pcsServiceIdentitiesIncluded": true where the earlier failed attempt had false. So the login looks fine this time, and the “error” that stopped the script was a false alarm from my check.

My check matched the word “locked” anywhere in the log, and Apple’s normal account data contains "locked": false. That was a second bug in my wait mode, and I’m fixing it now. I’ll make it look only for real error lines, and I’ll hide the raw JSON so the output shows just the status lines.

Fixed and pushed. I tested the new error patterns on sample lines. "locked": false is now ignored, while a real ERROR line and Apple’s “locked for security” message both stop the script. I haven’t run the whole script, since it needs your session.

On junipernine2:

cd ~/photo-curator && git pull
export ICLOUD_USER='your real Apple ID email'
scripts/apple_pull.sh wait

What it will show now:

Only the status lines, such as “Processing user”, “Authenticating” and the indexing message, rather than screens of JSON.
If the library is ready, it will say there was no indexing message and no errors. It should have started downloading the 20 trial photos at that point, so look for Downloading ... lines. Don’t trust the “no errors” message on its own.
If it’s still indexing, it will say so and sleep for 20 minutes.
If it hits a real error, it stops.

Please paste the status lines it prints. And once you see Downloading lines, the find listing from earlier will show whether the Live Photos came through as HEIC + MOV pairs.

(venv) aj9@junipernine2:~/photo-curator$ scripts/apple_pull.sh wait
[10:04:49] attempt 1 of 24
2026-10-09 10:04:50 INFO     Processing user: tenuoustoaster@gmail.com
2026-10-09 10:04:51 INFO     Apple iCloud Photo Library has not finished indexing yet
Still indexing; sleeping 20 minutes.    I will eave it and go walk the dog

Good plan. It’s working as intended: it correctly reports “still indexing” and will retry every 20 minutes without you watching, for up to about 8 hours.

Before you go:

Leave that terminal running. If you close it or the SSH connection drops, the loop stops. If you didn’t start it inside tmux, don’t close the window.
Don’t run other icloudpd commands in the meantime. Repeated logins from several places are the likeliest way to get your real account locked.
If it stops with an error, it will say so in the terminal. If it says no indexing message and no errors, look for Downloading ... lines. Then the 20 trial photos are in /media/aj9/Juniper13/photo-archive/originals/apple/.

When you’re back, paste the last few status lines. If it reaches the trial download, I’ll check the Live Photo pairing with you and then we’ll run full. Enjoy the walk.

the script errored = I've restarted it = it errored with 2026-10-09 10:24:53 INFO     Processing user: tenuoustoaster@gmail.com
2026-10-09 10:24:55 ERROR    library exception: Apple iCloud Photo Library has not finished indexing yet
2026-10-09 10:24:55 ERROR    Unknown library: PrimarySync
An error (not just indexing) occurred - stopping. Read the output above.

Debugging why a script halted on an indexing exception.

This isn’t a new error. It’s the same indexing problem, and my script mishandled it. This time icloudpd logged the indexing message as an ERROR line instead of INFO, with the follow-on “Unknown library: PrimarySync”, which is just the library not being available yet. My fix treated any ERROR line as a real failure, so it stopped. It should have recognised the indexing message first. I’m correcting that now.

Fixed and pushed. Wait mode now treats the indexing message as “keep waiting”, whether it’s logged as INFO or ERROR. It stops only for other real errors, such as a failed login or a locked account. I also noted the Unknown library: PrimarySync symptom in the runbook, so it’s recognisable next time.

To restart the wait, in tmux on junipernine2:

cd ~/photo-curator && git pull
export ICLOUD_USER='your real Apple ID email'
scripts/apple_pull.sh wait

It’s now about 45 minutes since indexing first showed up, and the diagnosis hasn’t changed: Apple hasn’t finished preparing the library for this kind of access. I can’t say when it will. If it’s still stuck after a few hours, there are two other routes:

Apple’s own data export. At https://privacy.apple.com you can request a copy of your photos. Apple prepares them as downloadable files, which can take days. It doesn’t depend on this indexing step, and it’s a legitimate fallback while icloudpd waits.
osxphotos from the Mac. Photos on the Mac would download the full-size originals from iCloud first, which takes time for 30GB but doesn’t use this web-access route.

I’d leave the wait running for now, and decide on a fallback if it’s still blocked by this afternoon.
