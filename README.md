# commendation

An [MCP](https://modelcontextprotocol.io) server that recommends **new** songs — never a song already in your library, meaning never a song already in Liked Music *or in any of your playlists*, not just the one you seeded from.

It's built to do better than a streaming service's built-in radio/autoplay by pooling multiple independent discovery signals (radio, related content, artist catalog expansion) and ranking candidates by how many of them agree, instead of trusting one black-box algorithm.

**Backend: YouTube Music (v1).** Commendation is designed as a general recommendation engine, not tied to one service — v1 is built entirely against YouTube Music (via `ytmusicapi`). Spotify support is planned as a second backend; see `PLAN.md`'s "v3 — Multi-provider support" section for the design questions around that.

## Tools

| Tool | Description |
| --- | --- |
| `recommend_from_song(video_id=None, song=None, artist=None, limit=20)` | Recommend new songs similar to a seed song. Pass `video_id` directly, or `song` (optionally with `artist`) to have the seed resolved via search — e.g. "songs that relate to Kryptonite by 3 Doors Down" needs no separate lookup first. |
| `recommend_from_playlist(playlist_id, limit=20, seed_sample_size=5)` | Recommend new songs based on an entire playlist (samples seed tracks from it). |
| `songs_by_artist(artist, limit=10)` | Return actual songs by a named artist — a direct catalog pull, not a similarity recommendation. |
| `refresh_library()` | Force-rebuild the cached library exclusion set. See [Library cache](#library-cache). |

All three tools guarantee every result is absent from Liked Music *and* from every one of your playlists, not just the one you seeded from (if any). `recommend_from_song` additionally never returns the seed song itself; `recommend_from_playlist` additionally never returns anything from the seed playlist even if that playlist somehow isn't in your library listing.

`songs_by_artist` is a different kind of tool from the other two: no scoring, no radio/related signals — just that artist's real catalog, with the same library-wide exclusion applied. It's a hard requirement, not best-effort: if fewer than `limit` qualifying songs exist, it returns however many were found (`found` in the response) rather than padding the list with substitutes. It never adds anything anywhere.

## Library cache

Every recommendation excludes anything already in your library, which means building a set of every
videoId in Liked Music plus all of your playlists. Measured against a real account (~1,100 liked songs,
28 playlists, ~1,550 playlist tracks) that costs **~20 seconds** — and v1 paid it on every single tool call.

That set is now cached on disk. Measured on the same account:

| | Before | After |
| --- | --- | --- |
| Building the exclusion set | 20.5s | 0.9s |
| `recommend_from_song` end to end | ~24s | 4.3s |
| `songs_by_artist` end to end | ~22s | 2.6s |

**Liking a song still takes effect immediately.** A cache hit re-fetches only the most recently liked
songs (one page, ~1s) and unions them in, so the novelty guarantee holds for the mutation you actually
make most. The case a cache hit can miss is a song added to some *other* playlist within the TTL — call
`refresh_library()` after doing that if it matters, e.g. right after a playlist-management tool adds tracks.

If the top-up fetch fails, the cached set is used as-is rather than failing the call — a slightly older
exclusion set beats no recommendation, the same partial-results philosophy used for discovery signals.

| Env var | Default | Meaning |
| --- | --- | --- |
| `COMMENDATION_CACHE_PATH` | `~/.commendation/library_cache.json` | Where the cached set lives (~22 KB). |
| `COMMENDATION_CACHE_TTL` | `21600` (6 hours) | How long a cached set stays usable. **Set to `0` to disable caching** and rebuild on every call. |

The cache is written atomically (temp file + rename), and a missing, unreadable, malformed or expired
cache is treated as a miss rather than an error — worst case you pay the ~20s rebuild v1 always paid.

**Not included (v1):** BPM/tempo-based comparison. YouTube Music doesn't expose tempo data, so this needs a second data source (e.g. a third-party BPM API) — a stretch goal for a future version, not part of this build. See `PLAN.md` for the full design rationale.

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Authenticate (YouTube Music)

There's no official YouTube Music API, so `ytmusicapi` authenticates by reusing headers from your logged-in browser session.

1. Open [music.youtube.com](https://music.youtube.com) in **Firefox** (recommended — its raw-header copy is more reliable than Chrome's) while logged in.
2. Open DevTools (`Cmd+Option+I` / `F12`) → **Network** tab → filter by `browse`.
3. Click into a playlist, or reload the page, to trigger a `browse` POST request.
4. Click that request → **Headers** tab → toggle **Raw headers** → select and copy the whole block.
5. Paste it into a new file named `raw_headers.txt` in the project root and save.
6. Run:
   ```bash
   python scripts/setup_auth_from_file.py
   ```
   This writes `headers_auth.json` and deletes `raw_headers.txt`.

Alternatively, `python scripts/setup_auth.py` does the same thing via an interactive terminal prompt instead of a file, if you prefer to paste directly.

**`headers_auth.json` is equivalent to your logged-in session — never commit it or share it.** It's already gitignored.

Verify auth works and sanity-check recommendations before going further:

```bash
python scripts/test_recommend.py
```

These headers expire/rotate periodically. If tools start failing with an auth error, redo this step.

### 3. Add to Claude Code

```bash
claude mcp add commendation -s user \
  -e COMMENDATION_AUTH_PATH="$(pwd)/headers_auth.json" \
  -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

`-s user` makes it available in any Claude Code session, not just this directory. Use absolute paths for the python interpreter, `server.py`, and `COMMENDATION_AUTH_PATH` since the server can be launched from any working directory.

For other MCP clients (Claude Desktop, etc.), point them at the same command and env var using their respective config format.

## Testing

Unit tests (`tests/`) cover the pure logic — normalization, scoring, ranking, exclusion filtering, library-cache behaviour (hits, misses, expiry, corruption, top-up, write failures), artist/song search resolution, error translation, and every tool end-to-end (happy path, signal failures, shortfalls, validation errors) — against a hand-rolled fake YTMusic client. No network access or `headers_auth.json` required. A `conftest.py` fixture redirects the library cache to a temp path for every test, so runs never touch your real cache.

```bash
pip install -e ".[dev]"
pytest
```

Check coverage with:

```bash
pytest --cov=server --cov-report=term-missing
```

`server.py` is at 98% line coverage (77 tests). The uncovered lines are `_client()`'s real `YTMusic()` construction and the `if __name__ == "__main__"` entrypoint — neither meaningfully testable without a live auth session or actually running the server as a process — plus two branches in `_artist_names_match`/`_filter_same_artist`.

`scripts/test_recommend.py` is a separate, complementary smoke test that hits your real account (see Setup step 2) to sanity-check that auth and live recommendations actually work.

## How recommendations are ranked

For each seed song, candidates are pulled from three independent signals:

1. **Radio** — YouTube Music's own autoplay/radio for that song.
2. **Related** — a separate "related content" signal, algorithmically distinct from radio.
3. **Artist expansion** — the seed artist's own other songs, plus top songs from a couple of their related artists.

A candidate's score is how many distinct (seed, signal) combinations surfaced it — the more independent signals agree, the higher it ranks. Every result includes a `sources` field showing which signals surfaced it, so recommendations are explainable rather than a black box.

Liked Music and every playlist in your library are excluded last, always, as a hard filter — no recommendation can ever be a song you've already liked or already saved anywhere.

## Error handling

Tool calls translate common failure modes into clear messages instead of raw tracebacks:

- Missing/expired/malformed auth → tells you to rerun `scripts/setup_auth_from_file.py`.
- Rate limiting (HTTP 429) → tells you to wait and retry.
- Gated/restricted content → reported as unavailable rather than crashing.
- Network errors → reported directly.
- If an individual signal (radio, related, or artist expansion) fails for a given seed, that signal is silently skipped for that seed rather than failing the whole recommendation.

## License

MIT — see [LICENSE](LICENSE).
