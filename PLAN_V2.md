# Commendation v2 — Mood-Aware Recommendation

> Design doc for v2. Read alongside `PLAN.md` (v1 design + v3 multi-provider notes).
> Every number in "Measured ground truth" below was probed against the real account on 2026-08-19,
> not estimated. Re-probe before trusting them after a few months.

## Thesis

v1 answers *"what is similar to this seed song?"* It's a similarity engine with a novelty guarantee.

v2 answers a different question: **"what does this person need to hear right now?"**

That is not a filter bolted onto v1. Similarity is a property of a *song pair*; mood is a property of a
*person at a moment*. The engine needs to model both, and the mood side needs three things v1 has none of:

1. A **mood representation** the tools, the index, and Claude can all speak.
2. **Mood knowledge about songs** — YouTube Music exposes no audio features whatsoever, so this must be built.
3. **Mood knowledge about the user** — their state right now, and their personal dialect of words like "chill."

BPM was the v1 stretch goal. It should be *demoted*, not built. See "Why not BPM" below.

---

## Measured ground truth

Probed live against the account (`headers_auth.json`) on 2026-08-19.

### What YouTube Music gives us

| Source | Result | Verdict |
| --- | --- | --- |
| `get_song(videoId)` | `videoDetails` has only title, author, `lengthSeconds`, `viewCount`, `musicVideoType` | **No audio features. None.** No tempo, key, valence, energy, danceability. |
| `get_mood_categories()` | 13 moods & moments, 27 genres | **The unlock.** First-party mood taxonomy, free. |
| `get_mood_playlists(params)` | **2,223 playlists** across the 13 moods (4.4s to enumerate all 13) | A large, free, mood-labeled corpus. |
| `get_lyrics(browseId)` | Works. Sample track returned 1,463 chars of real lyrics | **The deepest signal available.** Emotional content in plain text. |
| `get_history()` | 200 items, newest-first, with `videoId`, `likeStatus`, `inLibrary`, `duration` | Mood-sensing input — with a caveat, below. |
| `get_tasteprofile()` | 1,196 artists | Taste breadth, weak mood value. |

Mood playlist distribution — this is the corpus v2 is built on:

```
Chill      358    Commute    379    Energize   314    Feel good  220
Party      205    Romance    150    Focus      148    Christmas  131
Halloween  113    Workout     86    Sleep       51    Sad         41
Gaming      27
                                            TOTAL: 2,223 playlists
```

### The user's library

| | |
| --- | --- |
| Liked Music | ~1,100 tracks (7.4s to fetch) |
| Playlists | 28, holding 1,554 tracks (12.0s to fetch all) |
| Unique corpus | ~2,300–2,600 tracks |
| Genre buckets already curated by hand | `C - Bollywood/Hindi`, `C - Punjabi`, `C - Country`, `C - Hip-Hop & Rap`, `C - Electronic & Dance`, `C - Pop`, `C - R&B & Soul`, `C - Reggae & Dancehall`, `C - Rock & Alternative`, `C - Other / Mixed` |

**Every single v1 tool call rebuilds that exclusion set from scratch — ~20 seconds of API calls before any
recommendation work begins.** That is a v2 blocker regardless of mood, and Phase 0 fixes it.

### The coverage probe — the finding that shapes everything

Crawled a random 60 of the 2,223 mood playlists (2.7% of the atlas, 4,531 unique tracks, 41s) and
intersected against the ~1,100 liked tracks:

```
Coverage from a 2.7% sample:  43 / 1,061 liked tracks  =  4.1%
Mood labels that landed:      Romance 14, Feel good 14, Chill 9, Party 3,
                              Focus 1, Energize 1, Sleep 1, Sad 1
Tracks carrying >1 label:     1
```

Two conclusions, and they set the whole architecture:

1. **A full crawl will not cover this library.** Coverage grows sublinearly (popular tracks repeat across
   playlists), and the misses will concentrate exactly where this user listens most — Punjabi, Bollywood,
   Reggae/Riddim. YouTube's mood playlists are heavily English-language-pop-centric. Realistic expectation
   after a full crawl: **30–60% of this library, with the gap non-random.** Phase 1 must measure this for
   real and gate on it.
2. **Playlist membership alone yields ~1 mood label per track.** That gives you anchor-snapping, not a
   nuanced mood vector. A track tagged only "Chill" is indistinguishable from every other "Chill" track.

Therefore the lyrics + LLM layer is **not a nice-to-have refinement — it is load-bearing.** The atlas is
the cheap broad prior; language understanding is what actually produces mood resolution and covers the
non-English catalog. Plan accordingly.

---

## The mood model

A flat tag list ("sad", "hype", "chill") can't do distance, blending, or trajectories. Use a small
continuous vector so mood becomes geometry.

### Axes

| Axis | Range | Low end ←→ High end | Why it earns a slot |
| --- | --- | --- | --- |
| `valence` | -1..1 | despairing ←→ euphoric | The primary emotional sign. Russell's circumplex. |
| `energy` | 0..1 | still ←→ frantic | Arousal. The other circumplex axis. |
| `tension` | 0..1 | resolved/warm ←→ anxious/aggressive | **Separates angry from excited.** Both are high-energy, low-and-high valence respectively, but "aggressive workout rap" and "joyful party pop" are not interchangeable, and 2 axes can't tell them apart. |
| `depth` | 0..1 | wallpaper ←→ lyric-forward, demands attention | Decides whether the user gets something to *think along with* or something to work behind. Focus vs. Sad both matter here. |

Plus two non-mood knobs the mood state *sets*:

- `familiarity_appetite` (0..1) — comfort vs. novelty. Low mood usually wants the known; this is why a pure
  discovery engine can feel hostile when someone's down. v1 is hard-wired to maximum novelty; v2 must be
  able to dial it back, which means **allowing already-known songs into results when the mood calls for it**
  — a deliberate, opt-in relaxation of v1's central guarantee, never the default. (See "Open decisions".)
- `context` (enum) — `workout | focus | commute | sleep | party | romance | driving | none`. Maps almost 1:1
  onto YouTube's own "Moods & moments", so it is free structurally.

### Anchors

Each of the 13 YT moods gets a hand-authored anchor vector. First draft, to be tuned against real data:

```
                valence  energy  tension  depth
Sad              -0.70    0.25     0.35    0.85
Chill             0.25    0.25     0.15    0.35
Sleep             0.10    0.05     0.05    0.15
Focus             0.05    0.35     0.20    0.10
Commute           0.30    0.55     0.30    0.40
Feel good         0.75    0.60     0.10    0.35
Romance           0.55    0.35     0.20    0.70
Energize          0.60    0.90     0.45    0.30
Workout           0.35    0.95     0.70    0.20
Party             0.70    0.85     0.30    0.15
Gaming            0.10    0.75     0.65    0.15
```

A track's atlas vector = confidence-weighted mean of the anchors of every mood playlist it appears in.
Confidence = number of distinct mood *playlists* (not moods) it was found in.

### Free sub-mood signal

Playlist *titles* inside a mood are themselves fine-grained labels. The "Sad" mood contains
`Burn the Photos`, `Country Breakup`, `Hip Hop Heartbreak`, `End of the Road: Classic R&B Breakup`,
`Deal With It`. That is heartbreak-vs-resignation-vs-defiance resolution, for free, from strings we already
fetch. Feed the titles to the LLM labeler as context rather than throwing them away.

---

## Four layers of song-mood knowledge

Layered cheapest-and-broadest first; each layer fills the one above's gaps. Every result is cached
permanently by `videoId` — a song's mood does not change.

### Layer 1 — The Mood Atlas (offline, free, broad)

Crawl `get_mood_categories()` → `get_mood_playlists()` → `get_playlist()` for all 2,223 playlists.
Store `videoId → {mood, playlist_title, playlist_id}` rows.

- Cost: ~2,250 API calls. At the measured 0.7s/playlist, **~25–45 minutes** with throttling.
- Run as a background script (`scripts/build_atlas.py`), resumable, checkpointed after every playlist,
  refreshed monthly. Never inline in a tool call.
- Rate limiting is the live risk — 2,000+ sequential calls is exactly what earns a 429. Throttle
  deliberately (1 req/s, jittered), honour `Retry-After`, and make resume-from-checkpoint a first-class
  path, not an afterthought.

**Gate:** after the first full crawl, print real coverage against the library. If it lands under ~35%,
Layer 1 is a weak prior rather than a backbone, and Phase 2 gets prioritised harder.

### Layer 2 — Lyrics

`get_watch_playlist(videoId)` returns a `lyrics` browseId → `get_lyrics()` returns plain text. Confirmed
working.

- Cost: **2 API calls per song.** Never do this for 300 candidates inline. Do it for (a) the user's own
  library as a one-time background pass, and (b) the top ~30 shortlisted candidates in a request.
- Not universally available: instrumentals, and expect thinner coverage on the regional catalog.

### Layer 3 — The LLM judge

Title + artist + genre + atlas labels + playlist titles + lyric excerpt → mood vector JSON.

- Model split: `claude-haiku-4-5` for bulk library labeling, `claude-sonnet-5` for the live shortlist
  where precision actually shows.
- This is what handles Punjabi and Hindi lyrics, irony, and songs the atlas never saw — i.e. exactly the
  gap the coverage probe exposed.
- Batch 20–40 tracks per request. One-time cost per song, cached forever. A full ~2,500-track library pass
  on Haiku is small — low single-digit dollars.
- **Must degrade gracefully with no API key**: fall back to atlas-only, and say so in the tool response
  rather than silently returning worse results.
- Optional cheap fallback instead of an LLM: the NRC VAD lexicon (~20k English words rated for
  valence/arousal/dominance). No API, no cost, no network — but English-only and blind to irony, so it
  cannot close the gap that matters here. Fine as a stopgap; not a substitute.

### Layer 4 — The personal mood map

Label the user's own ~2,500-track library with Layers 1–3, once, in the background. This produces the
thing that actually makes recommendations feel personal:

- **Their** "chill" centroid, not the generic one. When they say chill, resolve against what *they*
  actually play when chilled out.
- The hand-curated `C - *` playlists give a **genre prior** per track for free — which answers the open
  question in `PLAN.md` about cross-genre noise. A mood match that jumps from Punjabi to Christian gospel
  is technically correct and practically wrong; genre affinity becomes a ranking term.
- Enables mood-matched seeding, below — the single biggest quality lever in this plan.

---

## Mood sensing — reading the user

Three inputs, fused, in strict priority order.

### 1. What they say (highest weight)

Claude is already in the loop and is a far better mood parser than anything shipped in the server. The tool
contract should exploit that rather than duplicate it:

```
recommend_for_mood(
    feeling: str | None,        # free text, verbatim from the user
    vector: dict | None,        # Claude's structured read, if it wants to be explicit
    context: str | None,        # workout | focus | commute | ...
    ...
)
```

Accept both. `feeling` keeps the tool usable by any client; `vector` lets Claude pass a nuanced read
("wistful but wants to stay productive" → high depth, low-ish valence, mid energy, low tension) without
round-tripping through a lossy string. If both arrive, `vector` wins and `feeling` is retained for logging.

### 2. What they've been playing

`get_history()` gives 200 items, newest-first. Compute the mood centroid and the *trajectory* of recent
plays, then detect the patterns that actually mean something:

- Same song repeating → strong emotional signal, and the single most reliable one available.
- Valence drifting down across a session → they're sinking.
- A sharp energy spike → gearing up.
- One artist dominating (the probe showed a Joyner Lucas run) → they're in a specific headspace, and it
  has a name.

**Real limitation, stated plainly:** `played` is only `"Today"` / `"Yesterday"` — coarse buckets, no
wall-clock timestamps. You get *order*, not *hour*. Fix by snapshotting history on a schedule and stamping
observations with local time, which accumulates a genuine longitudinal log the API refuses to give.

### 3. Context

Local time of day and day of week, as a weak prior only. 7am Tuesday and 11pm Friday are different
requests even with identical words. Never let this override what the user actually said.

### The tool that makes this conversational

`read_my_mood()` returns the inferred state **with its evidence**, so Claude can open with:

> "You've had the same three Joyner Lucas tracks on loop since yesterday — that reads pretty heavy.
> Want something that sits there with you, or something that pulls you up?"

That exchange *is* the product. It's the difference between a recommender and something that feels like it
noticed. Ship the evidence, not just the verdict.

---

## Retrieval — mood-matched seeding

The lazy version of v2 is: run v1, then filter by mood. **Don't build that.** It fails badly — filtering a
Daft Punk radio for "melancholy" yields the least-danceable Daft Punk adjacent tracks, not melancholy music.

Instead, mood changes *where candidates come from*:

1. Resolve target mood → vector.
2. **Pick seeds from the user's own library nearest that vector** (Layer 4), 5–8 of them, weighted by their
   genre distribution so results don't collapse into one bucket.
3. Run v1's proven 3-signal generation per seed (radio / related / artist expansion) — unchanged, it works.
4. **Add a 4th signal: mood-playlist neighbours.** Pull tracks from atlas playlists whose vector is near the
   target. This is the discovery path that reaches outside the user's existing taste graph, which is exactly
   what radio-from-your-own-songs can never do.
5. Score = v1's convergence score **× mood fit × genre affinity × novelty weight**, with each term
   reported separately so results stay explainable.
6. Apply v1's library exclusion — unless `familiarity_appetite` is high and the user opted into comfort mode.

Step 2 is the heart of it. "I'm feeling nostalgic" should seed from the songs *this person* finds nostalgic,
which is a fact the system can only know because Layer 4 exists.

---

## Arcs — the differentiating feature

Don't return a mood-matched *set*. Return a mood-shaped *sequence*.

The iso-principle from music therapy: to move someone's affect, meet them where they are and shift
gradually. Jumping straight to upbeat songs when someone is low gets skipped — it reads as being told to
cheer up.

| Arc | Behaviour |
| --- | --- |
| `mirror` | Stay at their current mood. Validate it. The default when someone names a feeling. |
| `lift` | Start at current, interpolate to higher valence/energy across N tracks. |
| `settle` | Start at current, descend to calm. Evening wind-down. |
| `deepen` | Move further in. Sometimes you want to sit in it properly. |
| `hold` | Stay inside a context band, shaping energy as a curve — workout is warmup → peak → cooldown, not a flat wall of intensity. |

Implementation: compute a target vector per slot along the curve, then assign the best-fitting candidate to
each slot — greedy is fine to start, Hungarian assignment if it proves lumpy. Add sequencing constraints:
no same artist adjacent, no jarring energy jump between neighbours, cap tracks per artist across the set.

No streaming service's radio does this. It is the clearest reason for v2 to exist.

---

## Feedback and learning

Log every recommendation with the mood context it was served under, then close the loop.

**Explicit:** a `record_feedback(video_id, reaction)` tool — `loved | saved | skipped | wrong_mood`.
`wrong_mood` is the valuable one; it says the retrieval was fine and the *mood model* was off.

**Implicit, and better because it costs the user nothing:** a background job diffs prior recommendations
against subsequent `get_history()`. Did a recommended track get played? Played repeatedly? Liked? Added to
a playlist? That's ground truth with zero friction, and it's available precisely because history is
readable.

What the loop adjusts:
- Personal anchor vectors drift toward what they actually accept in each mood.
- Artists repeatedly rejected in a given mood get downweighted *for that mood*, not globally.
- Per-mood novelty tolerance is learned rather than assumed.

---

## Architecture

v1 is a single ~500-line `server.py` with no persistence. v2 needs real structure — and it happens to be
the same restructuring `PLAN.md`'s v3 multi-provider work needs, so do it once, properly.

```
commendation/
  server.py            # thin MCP tool layer only
  provider/
    base.py            # Protocol — the v3 seam, defined now
    ytmusic.py         # everything ytmusicapi-specific
  mood/
    space.py           # vectors, axes, distance, interpolation
    anchors.py         # the 13 mood anchor vectors
    atlas.py           # crawl + query the mood-playlist index
    lyrics.py          # fetch + cache
    judge.py           # LLM labeling (degrades gracefully without a key)
    sense.py           # history + context → current mood
  rank.py              # scoring, now multi-term
  arc.py               # sequencing
  store.py             # SQLite
scripts/
  build_atlas.py       # ~30 min, resumable, cron-able
  label_library.py     # one-time + incremental
  snapshot_history.py  # timestamps history; cron every few hours
```

### Storage — SQLite at `~/.commendation/store.db`

| Table | Holds |
| --- | --- |
| `track` | videoId, title, artists, album, duration, genre_prior |
| `track_mood` | videoId, valence, energy, tension, depth, confidence, source (`atlas`/`lyrics`/`llm`), labeled_at |
| `atlas_membership` | videoId, mood, playlist_id, playlist_title |
| `library_snapshot` | the exclusion set + fetched_at — **shipped ahead of the rest, as a JSON file rather than SQLite; fold it in when this table lands** |
| `history_log` | videoId, observed_at (real local timestamp), position |
| `recommendation` | videoId, served_at, mood context, arc, slot, score terms |
| `feedback` | videoId, reaction, source (`explicit`/`inferred`), at |

Cache the exclusion set with a TTL (~6h) plus an explicit `refresh_library()` tool. This alone makes every
existing v1 tool roughly 20 seconds faster, and is worth shipping before any mood work lands.

---

## Tool surface

v1's three tools keep working unchanged — no breaking changes.

**New:**

| Tool | Purpose |
| --- | --- |
| `recommend_for_mood(feeling, vector, context, arc, limit, familiarity, genres)` | The headline tool. |
| `read_my_mood()` | Inferred current mood **plus the evidence for it**. Makes the conversation possible. |
| `explain_recommendation(video_id)` | Why this song, in mood terms — extends v1's `sources` honesty. |
| `record_feedback(video_id, reaction)` | Close the loop. |
| `index_status()` | Atlas/label coverage and freshness, so failures are visible instead of silent. |

**Extended:** `recommend_from_song` / `recommend_from_playlist` gain an optional `mood` filter, so "more like
this, but calmer" works.

---

## Why not BPM

The v1 plan listed BPM as the v2 stretch goal. It should be dropped to the bottom, for three reasons:

1. **YouTube Music has no tempo data at all** — confirmed by probing `get_song()`. It requires a whole
   third-party integration to obtain.
2. **The obvious sources are weak.** AcousticBrainz stopped collecting data in 2022 and is effectively
   frozen; matching YouTube tracks to MusicBrainz recording IDs is lossy, and coverage of Punjabi/Bollywood
   catalog will be poor. GetSongBPM covers tempo but tempo is not mood.
3. **BPM is a bad mood proxy anyway.** 140bpm covers both a rage track and a euphoric one. Tension and
   valence are what distinguish them, and lyrics carry that; tempo does not.

The user's own framing — *"more than just seeing the BPM"* — is correct, and the probe data backs it.
If tempo is still wanted later, it belongs as a **sequencing** input (smoothing transitions inside an arc),
not as a mood signal.

---

## Phasing

Each phase is independently shippable and useful on its own.

| Phase | Scope | Why here |
| --- | --- | --- |
| **0 — Foundation** | ~~Cached exclusion set~~ **(done)**; package split, SQLite store, provider seam still open | Unblocks everything. The cache shipped first and is measured at 20.5s → 0.9s; see `PLAN.md` build status. |
| **1 — Atlas + mood space** | `build_atlas.py`, anchors, `recommend_for_mood` v0 (atlas-only) | First real mood recommendations, no API key needed. **Gate: measure true library coverage.** |
| **2 — Language layer** | Lyrics fetch + LLM judge + caching | Closes the coverage gap the probe found. Where mood resolution actually becomes good. |
| **3 — Personal map** | Label the library, mood-matched seeding, genre affinity | The step where results start feeling personal rather than correct. |
| **4 — Sensing** | `read_my_mood`, history snapshot cron, context priors | Enables the conversational opening. |
| **5 — Arcs** | Sequencing, the 5 arc types | The differentiator. Needs 1–3 to be solid first. |
| **6 — Learning** | Feedback tools, implicit history diffing, anchor drift | Compounds only after there's usage to learn from. |

---

## Risks

- **Rate limiting.** 2,200+ sequential playlist fetches is the most likely thing to break. Throttle, jitter,
  checkpoint, resume. Never crawl inline.
- **Coverage gap is non-random.** Punjabi/Bollywood/Riddim are underserved by YT's mood playlists *and* by
  English lyric lexicons. This is the plan's main quality risk, and Phase 2 is the mitigation.
- **Auth expiry breaks background jobs silently.** Header auth rotates. Every cron script needs a health
  check and a visible failure path; `index_status()` should surface staleness.
- **Cold start.** Phases 1–3 need a full crawl and a library labeling pass before quality shows. Budget
  roughly an hour of background compute before judging results.
- **Mood inference will sometimes be wrong.** Design for it: always show evidence, always let the user
  correct, never assert a feeling as fact. "That reads pretty heavy — right?" not "You are sad."
- **Privacy.** Lyrics, history and mood inferences are personal. Everything stays in local SQLite; the only
  thing that ever leaves the machine is (optionally) titles and lyric excerpts to the Claude API for
  labeling. Make that opt-in and documented.

---

## Open decisions

Three forks that materially change the build. Recommendations given; user's call.

1. **Allow the LLM labeling layer?** It costs a few dollars one-time and sends song titles and lyric
   excerpts to the Claude API. *Recommendation: yes* — the coverage probe shows the atlas alone cannot
   carry this specific library. Keep it optional and degrade to atlas-only without a key.
2. **Third-party APIs (Last.fm tags, GetSongBPM)?** Last.fm crowd tags would add a genuinely independent
   mood signal with good long-tail coverage. *Recommendation: defer* — YT atlas + lyrics + LLM likely
   suffices, and each integration adds an auth surface and a failure mode. Revisit if Phase 2 disappoints.
3. **Stay read-only?** Arcs are ordered sequences, which want to become real playlists. v1 is deliberately
   read-only, with writes delegated to `ytmusic-mcp`. *Recommendation: stay read-only* — return the ordered
   list and let the orchestrator write it via `ytmusic-mcp`. Preserves the clean separation and v3 portability.
