# re-com

An [MCP](https://modelcontextprotocol.io) server that recommends **new** songs — never a song already in your library, meaning never a song already in Liked Music *or in any of your playlists*, not just the one you seeded from.

It's built to do better than a streaming service's built-in radio/autoplay by pooling multiple independent discovery signals (radio, related content, artist catalog expansion) and ranking candidates by how many of them agree, instead of trusting one black-box algorithm.

**Backend: YouTube Music (v1).** re-com is designed as a general recommendation engine, not tied to one service — v1 is built entirely against YouTube Music (via `ytmusicapi`). Spotify support is planned as a second backend; see `PLAN.md`'s "v3 — Multi-provider support" section for the design questions around that.

## Tools

| Tool | Description |
| --- | --- |
| `recommend_from_song(video_id=None, song=None, artist=None, limit=20, language=None, match_seed_tempo=False, ...)` | Recommend new songs similar to a seed song. Pass `video_id` directly, or `song` (optionally with `artist`). Supports [language](#language-filtering) and [tempo](#tempo-bpm) filters. Returns `{"songs": [...], "notes": [...], "filters": {...}}`. |
| `recommend_from_playlist(playlist_id, limit=20, seed_sample_size=5)` | Recommend new songs based on an entire playlist (samples seed tracks from it). |
| `songs_by_artist(artist, limit=10)` | Return actual songs by a named artist — a direct catalog pull, not a similarity recommendation. |
| `refresh_library()` | Force-rebuild the cached library exclusion set. See [Library cache](#library-cache). |
| `recommend_for_mood(feeling=None, vector=None, context=None, arc="mirror", limit=20, genres=None, language=None, bpm=None, ...)` | **v2.** Recommend new songs matching how you actually feel, shaped into a sequence that moves. See [Mood](#mood-aware-recommendations-v2). |
| `read_my_mood()` | **v2.** Infer your current mood from recent listening, *with the evidence for it*. |
| `explain_recommendation(video_id)` | **v2.** Why a song was picked, in mood terms. |
| `record_feedback(video_id, reaction)` | **v2.** `loved` / `saved` / `skipped` / `wrong_mood`. Rejections are never recommended again. |
| `index_status()` | **v2.** How much of the mood index exists, so gaps are visible instead of silent. |

All three tools guarantee every result is absent from Liked Music *and* from every one of your playlists, not just the one you seeded from (if any). `recommend_from_song` additionally never returns the seed song itself; `recommend_from_playlist` additionally never returns anything from the seed playlist even if that playlist somehow isn't in your library listing.

`songs_by_artist` is a different kind of tool from the other two: no scoring, no radio/related signals — just that artist's real catalog, with the same library-wide exclusion applied. It's a hard requirement, not best-effort: if fewer than `limit` qualifying songs exist, it returns however many were found (`found` in the response) rather than padding the list with substitutes. It never adds anything anywhere.

## Mood-aware recommendations (v2)

`recommend_from_song` answers *"what sounds like this?"*. `recommend_for_mood` answers a different
question: *"what does this person need to hear right now?"*

### Why this isn't just a filter

Running the v1 engine and filtering its results by mood does not work — filter a Daft Punk radio for
"melancholy" and you get the least danceable Daft-Punk-adjacent tracks, not melancholy music. So the mood
decides **where candidates come from**:

1. Resolve the mood to a vector.
2. Pick seeds from **your own library** that already sit near it.
3. Run v1's proven radio / related / artist expansion from those seeds.
4. Add a fourth signal: songs from YouTube's mood playlists near the target — the only path that reaches
   outside your existing taste graph.
5. Rank on signal agreement × mood fit, then **assign songs to slots along an arc**.

### The mood vector

| Axis | Range | Low ←→ high |
| --- | --- | --- |
| `valence` | −1…1 | despairing ←→ euphoric |
| `energy` | 0…1 | still ←→ frantic |
| `tension` | 0…1 | resolved ←→ anxious. **Separates angry from excited** — two axes can't tell aggressive workout rap from joyful party pop |
| `depth` | 0…1 | background wallpaper ←→ lyric-forward |

Pass `vector` for precision, `feeling` for free text (matched against a mood-word lexicon), or `context`
for one of YouTube's own moods. With none of them, the mood is inferred from your listening history.

### Arcs

A mood-matched *set* is the obvious thing to return and the wrong one. From music therapy's iso-principle:
to shift someone's mood you meet them where they are and move gradually — opening with upbeat songs when
someone is low just gets skipped.

| Arc | Behaviour |
| --- | --- |
| `mirror` | Stay where they are and validate it. Default. |
| `lift` | Start at their mood, rise gradually across the set. |
| `settle` | Descend to calm — an evening wind-down. |
| `deepen` | Go further in. |
| `hold` | Stay in a band with energy as a curve (a workout is warmup → peak → cooldown). |

### How a song's mood is known

YouTube Music exposes **no audio features at all** — no tempo, key, valence or energy (verified against
the live API; that's why BPM was dropped rather than built). So mood is assembled from four layers,
cheapest first, and the best available source for a song wins outright:

| Layer | What it is | Needs |
| --- | --- | --- |
| `llm` | Claude reads the lyrics. Handles any language, and irony. | Optional — `pip install -e ".[llm]"` |
| `lyrics` | Lyrics fetched and cached (2 API calls/song, incl. the negative result) | — |
| `atlas` | Membership in YouTube's own mood playlists — 1,592 listings, 65,438 tracks, 104,028 memberships | A crawl |
| `artist` | An artist's average mood, propagated to their unlabelled songs | Free |

**The atlas alone is not enough, and measurably so.** On this account a 60-playlist sample covered 4.1% of
the liked library, and the misses concentrate on the Punjabi, Bollywood and Reggae catalogue that
YouTube's English-centric mood playlists barely touch. Artist propagation is what closes most of that gap
without any API key; the Claude layer closes the rest.

After a full crawl, measured: **71.3% library coverage** — 553 songs from artist propagation, 480 from
playlist membership.

### Honesty about shortfalls

`limit` is a ceiling, not a guarantee. `recommend_for_mood`'s arc sequencer will
fill every requested slot from whatever's left in the candidate pool if you let
it, quality be damned -- asking for 100 with 7 songs that genuinely fit the mood
otherwise came back as 100, the other 93 being progressively worse guesses (an
unrated song still gets a placeholder fit score and can still win a slot).

Filler -- unrated, or rated but a poor fit -- is capped at 25% of `limit`.
Genuine matches (rated, with a real fit above the unrated baseline) are never
capped or dropped for this reason. Asking for 100 with 7 genuine matches
returns 32 (7 + 25), not 100. The result's `match_quality` field reports
`genuine`/`requested`/`fluff_cap`/`fluff_used`, and `notes` explains it in
plain language.

### Measuring quality

`scripts/quality_check.py` scores a fixed set of mood/arc cases so changes can be judged by number rather
than impression:

```bash
python scripts/quality_check.py --titles
python scripts/quality_check.py --distinctiveness 0   # A/B the seed scoring
```

Watch **cross-mood overlap**, not just mean fit. An early build scored a healthy 0.775 mean fit while
returning 70% the same songs for "heartbroken" and "angry"; fit alone couldn't see it. Current numbers:
mean fit 0.848, cross-mood overlap 0.064, 63 distinct songs across 80 slots.

### Setup

```bash
# 1. Crawl the mood atlas (~35 min, resumable, safe to interrupt)
python scripts/build_atlas.py

# 2. Label your library (steps 1-3 need no credentials beyond YouTube Music)
python scripts/label_library.py

# 3. Genre/language labels, for the language filter (~10-15 min)
python scripts/build_genres.py

# 4. Tempo, for BPM filtering (~0.4s per song)
python scripts/build_tempo.py

# 5. Optional: read lyrics with Claude to cover what the atlas missed
pip install -e ".[llm]" && ant auth login
python scripts/label_library.py --claude
```

Check progress any time with `python scripts/build_atlas.py --status`,
`python scripts/label_library.py --report`, or the `index_status()` tool.

Optionally, keep a real timeline of listening — `get_history()` reports only "Today"/"Yesterday", so
local timestamps are the only clock this system will ever have:

```
0 */3 * * * cd /path/to/re-com && .venv/bin/python scripts/snapshot_history.py
```

### Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `RECOM_DB_PATH` | `~/.recom/store.db` | Mood index, labels, history, feedback. |
| `RECOM_JUDGE_MODEL` | `claude-opus-5` | Model for lyric-based labelling. |
| `RECOM_JUDGE_EFFORT` | `low` | Effort level for that labelling. |
| `RECOM_JUDGE_BATCH` | `12` | Songs per labelling request. |

Everything mood-related is stored in local SQLite. The only thing that ever leaves the machine is,
optionally, song titles and lyric excerpts sent to the Claude API for labelling.

## Language filtering

*"Find songs like this Punjabi track, but only English ones."*

```python
recommend_from_song(song="Brown Munde", artist="AP Dhillon", language=["english"])
recommend_for_mood(feeling="hyped", exclude_languages=["punjabi", "hindi"])
```

Nothing in the YouTube Music API returns a language, so it's assembled in layers,
strongest first:

| Layer | Evidence | Weight |
| --- | --- | --- |
| `script` | Title written in Gurmukhi, Devanagari, Arabic, Hangul, Kana or Han | 100 |
| `library` | Your own playlist names (matched loosely — `Punjabu` counts) | 50 |
| `genre` | YouTube's genre-category pages | 10 |
| `genre` (English) | The same, but for anglophone genres | **1** |

**English is weighted at 1 on purpose.** YouTube files Punjabi and Hindi rap under
"Hip-hop", so counting an English-genre hit as a normal vote labelled Sidhu Moose Wala,
Karan Aujla and AP Dhillon as English. English is now what you get when *no*
language-bearing evidence exists, rather than something that can outvote real evidence.

Two behaviours worth knowing:

- **Unlabelled candidates are dropped by default.** Asking for English only is a request
  for a guarantee, and an unlabelled candidate from a Punjabi-seeded pool is probably
  Punjabi. The response always reports how many were dropped;
  `allow_unlabelled_language=True` keeps them.
- **Filtering alone isn't enough, so retrieval expands.** Seeding from a Punjabi song and
  filtering for English left 3 results out of 8 — the pool simply didn't contain more. The
  surviving songs are re-seeded to reach further into that language, and the response says
  when that happened. `expand_across_language=False` disables it.

This infers *language* from *genre*, which is approximate — "Dance & electronic" is often
instrumental, and "Reggae & caribbean" is usually English. Treat it as a strong hint.

## Tempo (BPM)

YouTube Music exposes no tempo data, so BPM comes from Deezer's public API — no key, no
auth, no attribution required.

```python
recommend_from_song(song="Kryptonite", artist="3 Doors Down", match_seed_tempo=True)
recommend_for_mood(context="Workout", bpm_min=120, bpm_max=140)
```

- `bpm` biases ranking toward a tempo; `bpm_min`/`bpm_max` bound it hard.
- `match_seed_tempo=True` uses the seed song's own BPM.
- **Half- and double-time count as close.** 170bpm drum-and-bass and 85bpm hip-hop share a
  pulse; treating them as opposites would be musically wrong.
- Tempo is **never propagated by artist**, unlike mood — an artist's songs share a
  sensibility, not a BPM. Propagating it would be inventing data.

**Coverage is uneven, and the response says so.** Measured across the whole library —
541 of 1,495 songs (36.2%):

| | | | |
| --- | --- | --- | --- |
| Rock & Alternative | 67% | Hip-Hop & Rap | 47% |
| R&B & Soul | 64% | Electronic & Dance | 38% |
| Pop | 60% | **Bollywood/Hindi** | **16%** |
| Country | 56% | **Punjabi** | **6%** |
| Reggae & Dancehall | 49% | | |

The misses are genuine: those songs resolve to the correct track on Deezer and simply carry
`bpm: 0`. So **a song with unknown BPM is never dropped**, only left unscored on tempo —
dropping them would quietly delete whole languages from the results.

Build the index with `python scripts/build_tempo.py` (~0.4s/song, cached permanently
including the misses).

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
| `RECOM_CACHE_PATH` | `~/.recom/library_cache.json` | Where the cached set lives (~22 KB). |
| `RECOM_CACHE_TTL` | `21600` (6 hours) | How long a cached set stays usable. **Set to `0` to disable caching** and rebuild on every call. |

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
claude mcp add re-com -s user \
  -e RECOM_AUTH_PATH="$(pwd)/headers_auth.json" \
  -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

`-s user` makes it available in any Claude Code session, not just this directory. Use absolute paths for the python interpreter, `server.py`, and `RECOM_AUTH_PATH` since the server can be launched from any working directory.

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

197 tests, 97% line coverage across the whole project. `tests/test_v2.py` covers the mood engine — the vector space, arcs, label resolution and artist propagation, the atlas crawler's resume and rate-limit behaviour, lyric caching, mood sensing, the Claude judge (against a fake client), and every v2 tool end to end. What remains uncovered is `_client()`'s real `YTMusic()` construction and the `if __name__ == "__main__"` entrypoints, neither meaningfully testable without a live auth session.

`conftest.py` redirects both the library cache and the SQLite store to temp paths for every test, so runs never touch your real data.

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
