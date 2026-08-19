"""Mood-driven recommendation.

The obvious way to build this is to run v1 and then filter the results by mood.
Don't: filtering a Daft Punk radio for "melancholy" returns the least danceable
Daft-Punk-adjacent tracks, not melancholy music. The mood has to decide *where
candidates come from*, not merely which survive.

So the flow is: read the mood, pick seeds from the listener's own library that
already sit near it, run v1's proven three-signal generation from those seeds,
add songs from mood playlists near the target, then rank on how many signals
agreed *and* how well each song fits the mood, and finally shape the result
into a sequence that moves (see arc.py).
"""

from typing import Any, Iterable

import arc as arc_module
import label
import moodspace
import sense
import signals
import store

SEED_COUNT = 6
CANDIDATES_PER_SLOT = 12
ATLAS_NEIGHBOUR_LIMIT = 400

# Bounding box for the SQL pre-filter before exact distances are computed.
_VALENCE_WINDOW = 0.4
_ENERGY_WINDOW = 0.3

# A song by an artist already in the library is a safer bet at the same mood
# fit -- taste is not uniform across a genre.
KNOWN_ARTIST_BOOST = 1.15
ATLAS_SOURCE = "mood-playlist"

# Free-text mood words. Claude is expected to pass a precise `vector` for
# anything nuanced; this exists so the tool still works when it doesn't, and
# for words the anchor names don't cover.
_FEELING_WORDS: dict[str, str | dict[str, float]] = {
    "sad": "Sad", "down": "Sad", "low": "Sad", "blue": "Sad", "depressed": "Sad",
    "heartbroken": "Sad", "heartbreak": "Sad", "lonely": "Sad", "grieving": "Sad",
    "miserable": "Sad", "crying": "Sad", "hurt": "Sad",
    "chill": "Chill", "relaxed": "Chill", "mellow": "Chill", "calm": "Chill",
    "laidback": "Chill", "easy": "Chill", "cruising": "Chill",
    "sleepy": "Sleep", "tired": "Sleep", "exhausted": "Sleep", "bedtime": "Sleep",
    "focus": "Focus", "focused": "Focus", "working": "Focus", "studying": "Focus",
    "concentrate": "Focus", "productive": "Focus", "coding": "Focus",
    "commute": "Commute", "driving": "Commute", "drive": "Commute", "road": "Commute",
    "happy": "Feel good", "good": "Feel good", "great": "Feel good", "joyful": "Feel good",
    "cheerful": "Feel good", "sunny": "Feel good", "upbeat": "Feel good",
    "romantic": "Romance", "love": "Romance", "tender": "Romance", "intimate": "Romance",
    "hyped": "Energize", "energized": "Energize", "pumped": "Energize", "amped": "Energize",
    "excited": "Energize",
    "workout": "Workout", "gym": "Workout", "lifting": "Workout", "running": "Workout",
    "training": "Workout",
    "party": "Party", "partying": "Party", "celebrating": "Party",
    "gaming": "Gaming",
    "angry": {"valence": -0.45, "energy": 0.85, "tension": 0.9, "depth": 0.45},
    "furious": {"valence": -0.55, "energy": 0.9, "tension": 0.95, "depth": 0.4},
    "rage": {"valence": -0.5, "energy": 0.9, "tension": 0.95, "depth": 0.35},
    "frustrated": {"valence": -0.4, "energy": 0.6, "tension": 0.8, "depth": 0.5},
    "nostalgic": {"valence": -0.05, "energy": 0.35, "tension": 0.2, "depth": 0.85},
    "wistful": {"valence": -0.2, "energy": 0.25, "tension": 0.2, "depth": 0.85},
    "bittersweet": {"valence": -0.15, "energy": 0.35, "tension": 0.25, "depth": 0.8},
    "anxious": {"valence": -0.4, "energy": 0.5, "tension": 0.85, "depth": 0.6},
    "stressed": {"valence": -0.35, "energy": 0.55, "tension": 0.85, "depth": 0.45},
    "overwhelmed": {"valence": -0.45, "energy": 0.45, "tension": 0.8, "depth": 0.6},
    "reflective": {"valence": 0.0, "energy": 0.2, "tension": 0.15, "depth": 0.9},
    "introspective": {"valence": -0.1, "energy": 0.2, "tension": 0.2, "depth": 0.9},
    "thoughtful": {"valence": 0.05, "energy": 0.25, "tension": 0.15, "depth": 0.85},
    "confident": {"valence": 0.6, "energy": 0.75, "tension": 0.45, "depth": 0.4},
    "powerful": {"valence": 0.5, "energy": 0.85, "tension": 0.6, "depth": 0.35},
    "restless": {"valence": -0.1, "energy": 0.7, "tension": 0.7, "depth": 0.45},
    "bored": {"valence": -0.15, "energy": 0.3, "tension": 0.35, "depth": 0.3},
}


def parse_feeling(text: str | None) -> dict[str, float] | None:
    """Map free text to a mood vector by matching known feeling words.

    Deliberately simple. The tool's `vector` argument is the precise path --
    Claude reads the sentence far better than a word list can, and "wistful but
    still wants to get things done" has no keyword.
    """
    if not text:
        return None
    words = "".join(c.lower() if (c.isalnum() or c.isspace()) else " " for c in text).split()
    hits = []
    for word in words:
        found = _FEELING_WORDS.get(word)
        if found is None:
            continue
        hits.append(moodspace.ANCHORS[found] if isinstance(found, str) else moodspace.vector(**found))
    if not hits:
        return None
    return moodspace.blend((vec, 1.0) for vec in hits)


def resolve_target(
    conn: Any,
    yt: Any | None = None,
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """Work out what mood to aim at, and be explicit about where it came from.

    Priority is what the listener said, then what they've been playing, then a
    weak time-of-day prior. Never let the clock overrule a person.
    """
    if vector is not None and moodspace.is_vector(vector):
        return {"target": moodspace.clamp(vector), "origin": "explicit", "evidence": []}

    if context and context.title() in moodspace.ANCHORS:
        return {
            "target": dict(moodspace.ANCHORS[context.title()]),
            "origin": "context",
            "evidence": [f"Using the {context.title()} profile."],
        }

    parsed = parse_feeling(feeling)
    if parsed is not None:
        return {"target": parsed, "origin": "feeling", "evidence": [f"Read from your words: {moodspace.describe(parsed)}."]}

    read = sense.read_mood(conn, yt)
    if read["vector"] is not None:
        return {"target": read["vector"], "origin": "history", "evidence": read["evidence"], "confidence": read["confidence"]}

    prior = sense.time_of_day_prior()
    return {
        "target": moodspace.vector(valence=0.15, energy=0.5 + prior["energy_bias"], tension=0.3, depth=0.4 + prior["depth_bias"]),
        "origin": "time-of-day",
        "evidence": [f"No mood given and nothing readable in your history — defaulting to a {prior['label']} profile."],
    }


def pick_seeds(conn: Any, target: dict[str, float], count: int = SEED_COUNT, genres: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Choose songs from the listener's own library that already sit near the mood.

    This is the heart of the whole design. "Something nostalgic" should branch
    out from the songs *this person* finds nostalgic, which is knowable only
    because their library has been labelled.
    """
    library = store.library_video_ids(conn)
    if not library:
        return []

    moods = label.resolve_many(conn, library)
    if not moods:
        return []

    genre_filter = {g.lower() for g in genres} if genres else None

    scored = []
    for video_id, entry in moods.items():
        track = store.get_track(conn, video_id) or {}
        if genre_filter:
            genre = label.genre_prior(conn, video_id)
            if not genre or genre.lower() not in genre_filter:
                continue
        scored.append(
            {
                "videoId": video_id,
                "title": track.get("title"),
                "artists": track.get("artists"),
                "fit": moodspace.fit(entry["vector"], target),
                "seed_score": moodspace.seed_score(entry["vector"], target, entry["confidence"]),
                "mood": entry["vector"],
            }
        )

    scored.sort(key=lambda s: -s["seed_score"])

    # Spread seeds across artists: six songs by one artist is a radio station,
    # not a mood.
    picked, seen_artists = [], set()
    for entry in scored:
        artist = label.primary_artist(entry["artists"])
        if artist and artist in seen_artists:
            continue
        if artist:
            seen_artists.add(artist)
        picked.append(entry)
        if len(picked) >= count:
            break
    return picked


def atlas_neighbours(conn: Any, target: dict[str, float], limit: int = ATLAS_NEIGHBOUR_LIMIT) -> list[dict[str, Any]]:
    """Songs from YouTube's mood playlists that sit near the target.

    The fourth signal, and the only one that reaches outside the listener's own
    taste graph -- radio seeded from their library can only ever circle it.
    """
    rows = conn.execute(
        "SELECT tm.video_id, tm.valence, tm.energy, tm.tension, tm.depth, tm.confidence, "
        "       t.title, t.artists, t.album "
        "FROM track_mood tm LEFT JOIN track t USING (video_id) "
        "WHERE tm.source = 'atlas' "
        "  AND tm.valence BETWEEN ? AND ? AND tm.energy BETWEEN ? AND ?",
        (
            target["valence"] - _VALENCE_WINDOW, target["valence"] + _VALENCE_WINDOW,
            target["energy"] - _ENERGY_WINDOW, target["energy"] + _ENERGY_WINDOW,
        ),
    ).fetchall()

    scored = []
    for row in rows:
        vector = moodspace.vector(
            valence=row["valence"], energy=row["energy"], tension=row["tension"], depth=row["depth"]
        )
        scored.append(
            {
                "videoId": row["video_id"],
                "title": row["title"],
                "artists": [a for a in (row["artists"] or "").split(" & ") if a],
                "album": row["album"],
                "mood": vector,
                "fit": moodspace.fit(vector, target),
                "sources": {ATLAS_SOURCE},
                "score": 1,
            }
        )
    scored.sort(key=lambda c: -c["fit"])
    return scored[:limit]


def _library_artists(conn: Any) -> set[str]:
    return {
        label.primary_artist(r["artists"])
        for r in conn.execute(
            "SELECT DISTINCT t.artists FROM track t JOIN library_track l USING (video_id)"
        )
        if label.primary_artist(r["artists"])
    }


def build(
    yt: Any,
    conn: Any,
    exclude: set[str],
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
    arc: str = "mirror",
    limit: int = 20,
    genres: Iterable[str] | None = None,
    use_history: bool = True,
) -> dict[str, Any]:
    """Produce a mood-shaped, ordered set of new songs."""
    resolved = resolve_target(conn, yt if use_history else None, feeling, vector, context)
    target = resolved["target"]

    seeds = pick_seeds(conn, target, genres=genres)
    notes = list(resolved.get("evidence", []))

    per_seed = []
    for seed in seeds:
        try:
            per_seed.append(signals._gather_seed_candidates(yt, seed["videoId"]))
        except Exception:  # noqa: BLE001 - one dead seed must not sink the request
            continue

    merged = signals._merge_and_score(per_seed)

    for candidate in atlas_neighbours(conn, target):
        existing = merged.get(candidate["videoId"])
        if existing is None:
            merged[candidate["videoId"]] = {
                "videoId": candidate["videoId"], "title": candidate["title"],
                "artists": candidate["artists"], "album": candidate["album"],
                "sources": {ATLAS_SOURCE}, "score": 1,
            }
        else:
            existing["sources"].add(ATLAS_SOURCE)
            existing["score"] += 1

    # A seed must never be recommended back. The library exclusion normally
    # covers this (seeds come from the library), but atlas_neighbours can
    # resurface one, and a caller may pass a narrower exclusion set.
    blocked = set(exclude) | {seed["videoId"] for seed in seeds}
    pool = {vid: c for vid, c in merged.items() if vid not in blocked}
    if not pool:
        return {
            "target": {k: round(v, 3) for k, v in target.items()},
            "target_origin": resolved["origin"],
            "described": moodspace.describe(target),
            "arc": arc, "seeds": seeds, "notes": notes + ["No candidates survived the library exclusion."],
            "songs": [],
        }

    moods = label.resolve_or_derive(conn, pool.keys())
    known_artists = _library_artists(conn)

    candidates = []
    for video_id, candidate in pool.items():
        entry = moods.get(video_id)
        artist = label.primary_artist(" & ".join(candidate["artists"] or []))
        boost = KNOWN_ARTIST_BOOST if artist and artist in known_artists else 1.0
        candidates.append(
            {
                "videoId": video_id,
                "title": candidate["title"],
                "artists": candidate["artists"],
                "album": candidate.get("album"),
                "sources": sorted(candidate["sources"]),
                "signal_score": candidate["score"],
                "mood": entry["vector"] if entry else None,
                "mood_source": entry["source"] if entry else None,
                "base_score": candidate["score"] * boost,
                "known_artist": bool(boost > 1.0),
            }
        )

    # Keep a generous shortlist so the sequencer has room to satisfy both the
    # arc and the per-artist cap.
    candidates.sort(key=lambda c: -c["base_score"])
    shortlist = candidates[: max(limit * CANDIDATES_PER_SLOT, 200)]

    slot_targets = arc_module.targets(target, arc, limit)
    ordered = arc_module.sequence(shortlist, slot_targets)

    # Remember what we served. Without this, explain_recommendation knows a
    # song was recommended but not what it was.
    store.upsert_tracks(conn, ordered)

    rated = sum(1 for song in ordered if song["rated"])
    if ordered and rated < len(ordered):
        notes.append(
            f"{rated}/{len(ordered)} picks have a mood label; the rest were ranked on signal "
            "agreement alone. Run scripts/label_library.py to raise that."
        )

    return {
        "target": {k: round(v, 3) for k, v in target.items()},
        "target_origin": resolved["origin"],
        "described": moodspace.describe(target),
        "arc": arc,
        "seeds": [
            {"title": s["title"], "artists": s["artists"], "fit": round(s["fit"], 3),
             "seed_score": round(s["seed_score"], 3)}
            for s in seeds
        ],
        "notes": notes,
        "songs": ordered,
    }
