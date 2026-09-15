# What's in here, and what isn't

This folder holds `schema.sql` — a real, well-formed Postgres/Supabase
schema someone pasted as part of a much larger "complete enterprise
platform" dump. It is **not connected to the running app**. See the header
comment in `schema.sql` for what would need to happen before it could be.

## What I deliberately did not integrate, and why

The dump this came from also included:

- **Python `BroadcastEngine`/`AIService`/`SecurityManager` classes** — every
  method (SDI init, NDI init, transcription, translation, forensic
  watermarking) was a `pass` stub. No logic to integrate — just method
  names and docstrings matching the product doc's feature list.
- **A Laravel PHP controller** (`App\Controllers\BroadcastController`) —
  assumes a full Laravel install (`auth()`, Eloquent models, service
  container bindings) that doesn't exist in this project. Our actual PHP
  (`registration/`) is plain PDO with no framework. Bolting this in would
  mean either faking a Laravel app around it or leaving dead code that
  can't run.
- **A React `BroadcastStudio` component** — assumes a different frontend
  entirely (React + hooks + CSS modules) than the vanilla JS room client
  that's actually running and tested (`frontend/room.js`).
- **Docker Compose and Kubernetes manifests** — reference container images
  (`prymax/backend-python:latest`, an SFU service, a GPU-passthrough
  "broadcast-gateway") that were never built or published anywhere.
  `docker-compose up` on this file would fail immediately trying to pull
  images that don't exist.

Copying these in would have made the project *look* more complete while
making it function exactly the same — or add files that error out the
moment someone tries to run them. That's worse than not having them, so
they're left out. If you want any of these built for real (a real Laravel
API layer, a real React frontend, a real Postgres-backed persistence
layer), say which one and I'll build and test that piece properly, the
same way the rest of this project was built.
