# Photo Curator

A self-hosted curation workbench for a family photo library, running on a home server alongside [Immich](https://immich.app).

Immich stores and presents the photos; Photo Curator does the tidying:

- picks the best frame from an iPhone Live Photo's video when the still is blurred
- finds duplicates, near-duplicates and bursts, and proposes a keeper
- scores photos for sharpness and exposure
- captions and tags photos with a local Qwen vision model via Ollama

Nothing is ever deleted automatically, and no photo leaves the house.

Built with FastAPI, SQLite and HTMX. See [SPEC.md](SPEC.md) for the full design.

## Status

Phase 0: specification.
