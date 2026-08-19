# Commendation — Design & Build Plan

> Reference doc for future Claude agents working on this project. Read this before writing any code here.
> This file may reference `ytmusic-mcp` (a sibling project) for context on prior decisions — nothing else in this
> repo (README, code, comments) should, since Commendation is meant to stand alone.

## Goal

Recommend genuinely **new** songs based on a seed song or seed playlist — better than a streaming service's built-in radio/autoplay, which the user has found mediocre and which resurfaces songs already liked. Commendation improves on radio by combining multiple independent discovery signals instead of trusting one algorithm, and guarantees novelty by hard-filtering anything the user has already liked (or, for playlist seeds, anything already in that playlist).

Commendation is meant to be a **general song-recommendation engine, not tied to one streaming service.** v1 is built entirely against YouTube Music (via `ytmusicapi`) since that's the account/library available today, but nothing about the goal, ranking approach, or tool contract is YouTube-specific — v3 (see below) plans to add Spotify as a second backend.

## Hard requirements (non-negotiable)

- Recommendations must **never** include a song already in Liked Music (playlist ID `LM`).
- When seeded by a playlist, recommendations must **never** include a song already in that seed playlist either — even if it isn't liked.
- v1 excludes BPM/tempo comparison entirely. YouTube Music exposes no tempo data. Revisit as a v2 stretch goal (needs a 3rd-party BPM source, e.g. GetSongBPM or AcousticBrainz — expect real coverage gaps).

## Why not just call YouTube's radio API directly

User feedback that shaped this design:
- YT Music's own radio/autoplay isn't good enough on its own.
- It resurfaces songs already in Liked Music, which defeats the purpose of a *discovery* tool.

Approach: treat radio as **one signal among several**, not the whole system, and always hard-filter out anything already liked.

## Architecture decision

- Standalone MCP server: **`commendation`**. Not a fork or extension of any other project — it has its own auth setup, its own scripts, its own README. (It happens to authenticate against the same YouTube account used elsewhere on this machine, but that's a user-level fact, not a code dependency — nothing in this repo imports or references another project.)
- Auth: same `ytmusicapi` browser-header approach as any `ytmusicapi`-based project — see this repo's own README for the exact steps. Auth file path is configurable via the `COMMENDATION_AUTH_PATH` env var (default `headers_auth.json` in the project root), never hardcoded.
- **Read-only recommendation engine.** It does not create or modify playlists — it only returns ranked recommendations. Saving results into an actual YouTube Music playlist is a separate concern for whatever's orchestrating this tool (e.g. Claude calling a playlist-management MCP tool alongside this one).
- Own `handle_errors` wrapper: auth-expired / rate-limit-429 / network-error → clean, actionable messages, no raw tracebacks.

## Candidate generation — multi-signal design

For each seed song, pull candidates from 3 independent `ytmusicapi` sources:

1. **Radio** — `get_watch_playlist(videoId=seed, radio=True, limit=25)` — YT's own autoplay signal.
2. **Related** — the `related` browseId returned alongside the radio call, fed into `get_song_related(browseId)` — a separate "related content" signal, algorithmically distinct from radio. Any item with a `videoId` across any of its returned sections counts (not just the "You might also like" section).
3. **Artist expansion** — `get_artist(channelId)` for the seed's primary artist → that artist's own top songs, **plus** their top 2 related artists' top songs (one more `get_artist` call each). This goes deeper into an artist's catalog and adjacent artists than radio ever surfaces.

If any individual signal call fails (`YTMusicError` / network error), that signal is skipped for that seed rather than failing the whole recommendation — partial results beat no results.

For **playlist-seeded** recommendations: randomly sample up to `seed_sample_size` tracks from the playlist (default 5; use the whole playlist if it's smaller) as seeds, run the same 3-signal generation per seed track, then pool all candidates together. Sampling is random per call (not cached/deterministic) — re-running the same playlist recommendation is expected to surface different picks over time, which is a feature for a discovery tool, not a bug.

## Ranking

No ML needed for v1 — simple, explainable scoring:

- **Score = number of distinct (seed, source) pairs that surfaced a candidate.** Per seed, a candidate can be hit by at most 3 sources (radio, related, artist). A song surfaced by radio *and* related *and* multiple seed tracks (playlist mode) racks up a higher score — that's a strong convergence signal.
- Every returned recommendation carries a `sources` field (the union of which signal types ever surfaced it) and a `score`, so results are explainable, not a black box.

## Exclusion filter (applied last, always)

- Pull the full Liked Music list once per call — `get_playlist("LM", limit=None)` (**not** `get_liked_songs()`, which defaults to only the most recent 100 — not nearly enough for a library this size). Build a videoId set, filter out any candidate in it.
- For playlist-seeded calls, also pull the seed playlist's own full track list and filter those out too.
- Dedupe candidates against each other (same videoId surfaced by multiple sources/seeds → one entry, sources unioned, score summed).

## Tools (v1)

- `recommend_from_song(video_id, limit=20) -> list[{videoId, title, artists, album, score, sources}]`
- `recommend_from_playlist(playlist_id, limit=20, seed_sample_size=5) -> list[{...}]`
- `songs_by_artist(artist, limit=10) -> {artist, requested, found, songs: [{videoId, title, artists, album}]}`
  — a deliberately different shape of tool from the two above. User-requested: "N songs by \[artist\]" is not
  a similarity recommendation, it's a direct catalog pull. Resolves the artist name via `search(filter="artists")`,
  pulls their real song catalog (`get_artist()`'s `songs.browseId` fed into `get_playlist` for full depth, falling
  back to the short `songs.results` preview if that lookup fails or there's no browseId), and excludes anything
  already in Liked Music **or in any of the user's playlists** — broader than `recommend_from_playlist`'s
  single-seed-playlist exclusion, since there's no single "seed playlist" here. No scoring/sources fields since
  there's no multi-signal ranking involved. Hard requirement (not best-effort): if fewer than `limit` qualifying
  songs exist after exclusion, returns however many were found — `found` vs `requested` tells the caller whether
  it fell short — rather than padding the list with worse substitutes. Never adds results anywhere (read-only,
  same as the rest of this server).
- *(v2, not in v1)* `compare_bpm(...)` — not implemented. Needs a 3rd-party tempo data source decision first.

## Open questions for v2 / future agents

- Which 3rd-party BPM/tempo API to use, and how to handle songs with no BPM coverage.
- Should ranking incorporate genre-taxonomy matching (the same idea used to bucket a liked-songs library into "Bollywood/Hindi", "Punjabi", genre playlists elsewhere) to avoid cross-genre noise in results?
- Rate-limit budget: playlist-seeded recs can trigger 25+ API calls per request (seeds × signals, including nested related-artist lookups). May need a call cap or caching layer if this proves slow or gets rate-limited in practice.
- Should mood/chart-based discovery (`get_mood_playlists`, `get_charts`) be added as a 4th signal for more diversity?
- Should `recommend_from_song` accept a search query instead of requiring a `videoId` up front (i.e. do the search internally)?

## v3 — Multi-provider support (Spotify)

Not started. Open design questions for whichever agent picks this up:

- **Provider abstraction.** `_gather_seed_candidates`, `_liked_video_ids`, etc. are currently written directly against `ytmusicapi`. v3 needs some kind of provider interface (e.g. a `Provider` protocol with `get_seed_candidates`, `get_liked_ids`, `get_playlist_tracks` methods) so the scoring/ranking/exclusion logic in `_merge_and_score` / `_finalize` — which is already provider-agnostic — can run over either backend unchanged.
- **Auth is a bigger difference than it looks.** YouTube Music auth here is a copy-pasted browser header (see README). Spotify uses real OAuth (client ID/secret, redirect URI, refresh token) via the Spotify Web API — a materially different setup flow, likely its own `scripts/setup_auth_spotify.py`.
- **Signal parity isn't guaranteed.** Spotify's Web API has artist top-tracks and (historically) a recommendations/related-artists endpoint, but Spotify has been actively deprecating/restricting several discovery endpoints for newer app registrations — check current API access levels before assuming parity with the YouTube Music 3-signal design above.
- **Tool contract question:** does `recommend_from_song`/`recommend_from_playlist` gain a `provider` argument, or does provider selection happen at the MCP-server-instance level (e.g. a separately configured `commendation-spotify` server)? Whichever it is, a single call should almost certainly stay within one provider — cross-provider merging (e.g. seeding from a YouTube Music playlist but recommending Spotify tracks) is out of scope unless a future agent has a concrete reason to want it.
- **IDs are provider-specific.** `video_id` is currently baked into the tool signatures and output (`videoId` field) as YouTube Music terminology. This session deliberately left that rename undone (see git history/PLAN discussion around 2026-08-19) rather than doing it speculatively — but it's the first thing to revisit once a second provider actually exists, since Spotify track IDs/URIs aren't YouTube video IDs.

## Build status

**Done:**
- Full scaffold in place: `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`, `scripts/setup_auth.py`, `scripts/setup_auth_from_file.py`, `scripts/test_recommend.py`.
- `server.py` implements both v1 tools (`recommend_from_song`, `recommend_from_playlist`) exactly per the design above, plus the shared `handle_errors` wrapper.
- `.venv` created, dependencies installed (`pip install -e .`), and `headers_auth.json` is in place (copied from an already-authenticated `ytmusicapi` session on this machine — no browser-auth flow needed to get running).
- Fixed one real bug found during smoke testing: `get_song_related` sections can contain plain strings (e.g. artist bio text) instead of track dicts, which crashed related-content parsing. Fixed with an `isinstance(item, dict)` guard in `_gather_seed_candidates`.
- **Verified working end-to-end against real account data:**
  - `recommend_from_song` seeded on Daft Punk's "One More Time" → 10 sensible recommendations, zero overlap with Liked Music.
  - `recommend_from_playlist` seeded on a real playlist → 15 recommendations, zero overlap with Liked Music or the seed playlist.

**Not done yet:**
- v2 items below are all still open.

**Since the above was written:**
- Git repo initialized, pushed to `github.com/umsachde/commendation` (`origin/main`).
- Registered with Claude Code via `claude mcp add commendation -s user ...` — connected.
- Added a real unit test suite (`tests/test_server.py`, 34 tests) against a fake YTMusic client — covers `_norm_track`, `_merge_and_score`, `_finalize`, `_gather_seed_candidates` (including the isinstance-guard regression and the related-artist expansion cap), `_liked_video_ids`, `handle_errors`, and both tools end-to-end. No network/auth needed; run with `pytest`.
- Found and fixed a real bug this way: `handle_errors` caught `YTMusicServerError` before `YTMusicGatedError`, and since the latter subclasses the former, the gated-content branch was dead code — gated errors always fell through to the generic "server error" message instead of the clearer gated-specific one. Reordered the `except` clauses (subclass before superclass) to fix.

**Aside (does not affect commendation, but happened during this build):** a `remove_from_playlist` tool was added to the separate `ytmusic-mcp` project to clean up a duplicate-track bug found in a "C - Country" playlist while smoke-testing `recommend_from_playlist` against real data. That's `ytmusic-mcp` maintenance, unrelated to commendation's own scope — mentioned here only so a future agent doesn't wonder why an unrelated commit landed mid-build.
