#2026-10-09

ME: I think the best starting point is to pull all the photos from Apple and Google

Agreed. Pulling everything in first is the safe order, because nothing downstream (Immich, the curator, Qwen) can start without it. One rule for this step: **copy only, delete nothing** from Apple or Google until the archive is verified.

**Apple: one thing decides the route**

If iCloud Photos has "Optimise Storage" turned on, your Mac only holds small previews of most photos, and the originals live in iCloud.

- **Mac Photos library holds the originals.** Run `osxphotos export` on the Mac. It keeps albums, favourites, edits and HEIC+MOV pairs.
- **Originals are only in iCloud.** Use `icloudpd` on junipernine2 to pull them straight from iCloud. It's a good fit for the server and doesn't need the Mac to download everything first. Apple's Advanced Data Protection, if you've turned it on, blocks this route. You'd need to export from the Mac instead.

**Google: Takeout**

- Request Photos only, and tick everything including albums. Choose 50GB zip parts for fewer files.
- Download **every part** into one folder on junipernine2 and leave them zipped. `immich-go` reads the zips directly, and keeping them lets you re-run the import if anything goes wrong.
- Takeout can take hours or days to prepare, so request it now and let it cook while we set up the rest.

**Landing zone on the server**

```
/data/photos-staging/
  apple/        # osxphotos or icloudpd output
  google/       # takeout-*.zip, untouched
  scans/        # prints later
```

Immich ingests from here later, and the staging copies stay as your original set, much like your "original set" backup for the cookbooks.

A few things I need from you:

1. Is "Optimise iPhone/Mac Storage" on, and is the iPhone library synced to the Mac mini, or is it iCloud-only?
2. Roughly how big are the Apple and Google libraries? The iCloud and Google storage pages will tell you.
3. Which path should the staging folder go under on junipernine2? Which drive has the space?

With those answers I'll write a short `docs/INGEST.md` and a small helper script in the repo for the route you pick.
