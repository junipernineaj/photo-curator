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