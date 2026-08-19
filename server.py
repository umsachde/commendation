"""MCP server that recommends new songs.

Combines multiple independent discovery signals (radio, related content,
artist catalog expansion) instead of trusting a single algorithm, and always
excludes anything already in the user's library. v1 is backed by YouTube
Music (via ytmusicapi); see PLAN.md for planned multi-provider support.
"""

import functools
import json
import os
import random
import time
from pathlib import Path
from typing import Any

import requests
from mcp.server.mcpserver import MCPServer
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicError, YTMusicGatedError, YTMusicServerError, YTMusicUserError

AUTH_PATH = os.environ.get("COMMENDATION_AUTH_PATH", "headers_auth.json")
AUTH_HELP = (
    f"YouTube Music auth at {AUTH_PATH} looks invalid or expired. "
    "Re-run scripts/setup_auth_from_file.py to refresh it (see README)."
)

# Rebuilding the library exclusion set costs ~20s of API calls (Liked Music
# plus every playlist), and every tool needs it. It's cached on disk instead;
# see _library_video_ids for how staleness is bounded.
_DEFAULT_CACHE_PATH = Path.home() / ".commendation" / "library_cache.json"
CACHE_PATH = Path(os.environ.get("COMMENDATION_CACHE_PATH") or _DEFAULT_CACHE_PATH)
# Seconds a cached exclusion set stays usable. <= 0 disables caching entirely.
CACHE_TTL = int(os.environ.get("COMMENDATION_CACHE_TTL", 6 * 60 * 60))
# How many of the most recently liked songs to re-check on every cache hit.
# ytmusicapi pages this, so the real count returned is typically ~2x.
RECENT_LIKES_LIMIT = 100

mcp = MCPServer("commendation")

_yt: YTMusic | None = None
_store_conn = None


def _store():
    """Lazily open the v2 mood store, shared across tool calls."""
    global _store_conn
    if _store_conn is None:
        import store

        _store_conn = store.connect()
    return _store_conn


def _client() -> YTMusic:
    global _yt
    if _yt is None:
        _yt = YTMusic(AUTH_PATH)
    return _yt


def handle_errors(fn):
    """Translate ytmusicapi/network failures into clear, actionable messages.

    Expired or malformed auth headers don't always raise a typed exception --
    a bad session can make YouTube's frontend return an HTML error page where
    ytmusicapi expects JSON, which surfaces as a raw JSONDecodeError.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"{AUTH_PATH} not found. Run scripts/setup_auth_from_file.py to authenticate."
            ) from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"YouTube Music returned an unexpected response. {AUTH_HELP}") from e
        except YTMusicGatedError as e:
            raise RuntimeError(f"This content is gated/restricted and unavailable: {e}") from e
        except YTMusicServerError as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                raise RuntimeError(AUTH_HELP) from e
            if "HTTP 429" in msg:
                raise RuntimeError(
                    "YouTube Music is rate-limiting requests right now. Wait a bit and try again."
                ) from e
            raise RuntimeError(f"YouTube Music server error: {msg}") from e
        except YTMusicUserError as e:
            raise RuntimeError(str(e)) from e
        except YTMusicError as e:
            raise RuntimeError(f"YouTube Music error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error reaching YouTube Music: {e}") from e
        except ValueError as e:
            # Our own argument validation (e.g. an unknown arc name). These are
            # actionable messages already -- they just need to arrive as a
            # clean tool error rather than a raw traceback.
            raise RuntimeError(str(e)) from e

    return wrapper


# Candidate generation lives in signals.py, shared with the mood engine.
# Re-exported here so existing callers and tests keep their import paths.
from signals import (  # noqa: E402
    _RELATED_ARTISTS_TO_EXPAND,
    _SIGNAL_ERRORS,
    _SOURCE_ARTIST,
    _SOURCE_RADIO,
    _SOURCE_RELATED,
    _artist_names_match,
    _filter_same_artist,
    _finalize,
    _gather_seed_candidates,
    _merge_and_score,
    _norm_track,
)

def _liked_video_ids(yt: YTMusic) -> set[str]:
    # get_liked_songs() defaults to only the most recent 100 items -- not
    # enough for a real library. get_playlist("LM", limit=None) fetches all.
    liked = yt.get_playlist("LM", limit=None)
    return {t["videoId"] for t in liked.get("tracks", []) if t.get("videoId")}


def _recent_liked_video_ids(yt: YTMusic) -> set[str]:
    """The most recently liked songs only -- one page, ~1s instead of ~7s.

    Newly liked songs land at the top of Liked Music, so this is what keeps a
    cached exclusion set honest about the mutation users actually make most.
    """
    recent = yt.get_playlist("LM", limit=RECENT_LIKES_LIMIT)
    return {t["videoId"] for t in recent.get("tracks", []) if t.get("videoId")}


def _build_library_video_ids(yt: YTMusic) -> set[str]:
    """Full rebuild: Liked Music plus every playlist the user owns. ~20s."""
    ids = set(_liked_video_ids(yt))
    for pl in yt.get_library_playlists(limit=None):
        playlist_id = pl.get("playlistId")
        if not playlist_id or playlist_id == "LM":
            continue
        try:
            full = yt.get_playlist(playlist_id, limit=None)
        except _SIGNAL_ERRORS:
            continue
        ids |= {t["videoId"] for t in full.get("tracks", []) if t.get("videoId")}
    return ids


def _read_cache() -> tuple[set[str], float] | None:
    """Return (ids, fetched_at) if a usable, unexpired cache exists, else None.

    A cache that is missing, unreadable, malformed, or expired is simply a
    miss -- never an error. The worst case is the ~20s rebuild we already had.
    """
    if CACHE_TTL <= 0:
        return None
    try:
        raw = json.loads(CACHE_PATH.read_text())
        fetched_at = float(raw["fetched_at"])
        ids = {v for v in raw["video_ids"] if isinstance(v, str)}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if time.time() - fetched_at > CACHE_TTL:
        return None
    return ids, fetched_at


def _write_cache(ids: set[str]) -> float:
    """Persist the exclusion set, returning the timestamp recorded for it.

    Written via a temp file + rename so an interrupted write can't leave a
    half-written cache behind. Failure to write is non-fatal -- the caller
    already has the data it needs.
    """
    fetched_at = time.time()
    payload = json.dumps({"fetched_at": fetched_at, "video_ids": sorted(ids)})
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(CACHE_PATH)
    except OSError:
        pass
    return fetched_at


def _library_video_ids(yt: YTMusic, force_refresh: bool = False) -> set[str]:
    """Every videoId already in the user's library: Liked Music plus every
    playlist they own, not just one seed playlist.

    Cached on disk (see CACHE_PATH / CACHE_TTL) because a full rebuild costs
    ~20s and every tool call needs it. On a cache hit the most recently liked
    songs are still fetched fresh (~1s) and unioned in, so liking a song takes
    effect immediately rather than waiting out the TTL. Adding a song to some
    other playlist is the case a hit can miss; refresh_library() forces a
    rebuild for that.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            ids, _ = cached
            try:
                return ids | _recent_liked_video_ids(yt)
            except _SIGNAL_ERRORS:
                # Top-up is an optimization, not a correctness requirement --
                # the cached set is still a valid (if slightly older) answer.
                return ids

    ids = _build_library_video_ids(yt)
    _write_cache(ids)
    return ids


def _resolve_artist(yt: YTMusic, artist: str) -> dict[str, Any] | None:
    """Resolve a free-text artist name to a search result carrying its
    channelId (`browseId`), or None if nothing matched."""
    results = yt.search(artist, filter="artists", limit=1)
    return results[0] if results else None


def _resolve_song_video_id(yt: YTMusic, song: str, artist: str | None = None) -> str | None:
    """Resolve a free-text song title (optionally with an artist name) to a
    videoId via search, so callers don't need to pre-resolve one themselves.

    Prefers a search result whose artist name matches `artist` (loosely --
    substring match, case-insensitive, since search titles/artist credits
    vary in exact formatting); falls back to the top search hit otherwise.
    """
    query = f"{song} {artist}" if artist else song
    results = yt.search(query, filter="songs", limit=10)
    if not results:
        return None
    if artist:
        artist_lower = artist.lower()
        for r in results:
            names = [a.get("name", "").lower() for a in (r.get("artists") or [])]
            if any(artist_lower in n or n in artist_lower for n in names if n):
                return r.get("videoId")
    return results[0].get("videoId")


def _artist_song_catalog(yt: YTMusic, channel_id: str) -> list[dict[str, Any]]:
    """Pull an artist's actual song catalog -- not similarity candidates.

    get_artist()'s own "songs" section is just a short top-songs preview.
    Its browseId points to the artist's full "Songs" playlist on YT Music,
    which is fetched for real catalog depth; the preview is a fallback if
    that lookup is unavailable.
    """
    info = yt.get_artist(channel_id)
    songs = info.get("songs") or {}
    browse_id = songs.get("browseId")
    if browse_id:
        try:
            full = yt.get_playlist(browse_id, limit=None)
        except _SIGNAL_ERRORS:
            full = None
        if full and full.get("tracks"):
            return full["tracks"]
    return songs.get("results") or []


# --- tools -------------------------------------------------------------


@mcp.tool()
@handle_errors
def recommend_from_song(
    video_id: str | None = None,
    song: str | None = None,
    artist: str | None = None,
    limit: int = 20,
    same_artist_only: bool = False,
) -> list[dict[str, Any]]:
    """Recommend new songs similar to a seed song.

    Seed the search either with a known `video_id`, or with `song` (a free-text
    title, optionally narrowed with `artist`) to have the seed resolved via
    search internally -- e.g. "10 songs that relate to Kryptonite by 3 Doors
    Down" needs no separate lookup first. Exactly one of `video_id` or `song`
    must be given.

    Combines YouTube Music's radio, its separate "related" signal, and the
    seed artist's own catalog plus related artists' catalogs, then ranks by
    how many independent signals agreed on each candidate. Never returns the
    seed song itself, and never returns a song already in Liked Music or in
    ANY of the user's playlists.

    By default candidates can come from OTHER artists too (radio/related
    signals surface stylistically similar tracks, not just the seed artist's
    own catalog) -- pass `same_artist_only=True` to keep only songs credited
    to the seed's own artist(s), e.g. for "recommend songs BY artist X similar
    to song Y" requests.

    The library exclusion set is cached for speed; newly liked songs are
    always honoured, but call refresh_library() after adding songs to a
    playlist by other means.
    """
    yt = _client()
    if not video_id:
        if not song:
            raise RuntimeError("Provide either video_id or song (optionally with artist).")
        video_id = _resolve_song_video_id(yt, song, artist)
        if video_id is None:
            desc = f"{song!r} by {artist!r}" if artist else repr(song)
            raise RuntimeError(f"No song found matching {desc}.")

    seed_artist_names: list[str] = []
    candidates = _gather_seed_candidates(yt, video_id, seed_artist_names)
    merged = _merge_and_score([candidates])
    if same_artist_only:
        merged = _filter_same_artist(merged, seed_artist_names or ([artist] if artist else []))
    exclude = _library_video_ids(yt)
    return _finalize(merged, exclude, limit)


@mcp.tool()
@handle_errors
def recommend_from_playlist(playlist_id: str, limit: int = 20, seed_sample_size: int = 5) -> list[dict[str, Any]]:
    """Recommend new songs based on an entire playlist.

    Randomly samples up to seed_sample_size tracks from the playlist as seeds
    (the whole playlist if it's smaller), runs the same multi-signal
    candidate generation as recommend_from_song for each, and pools/ranks the
    results. Never returns a song already in Liked Music, already in the
    source playlist, or already in ANY other of the user's playlists.

    The library exclusion set is cached for speed; newly liked songs are
    always honoured, but call refresh_library() after adding songs to a
    playlist by other means.
    """
    yt = _client()
    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks = [t for t in playlist.get("tracks", []) if t.get("videoId")]
    if not tracks:
        return []

    sample = tracks if len(tracks) <= seed_sample_size else random.sample(tracks, seed_sample_size)

    per_seed = [_gather_seed_candidates(yt, t["videoId"]) for t in sample]
    merged = _merge_and_score(per_seed)

    exclude = _library_video_ids(yt) | {t["videoId"] for t in tracks}
    return _finalize(merged, exclude, limit)


@mcp.tool()
@handle_errors
def songs_by_artist(artist: str, limit: int = 10) -> dict[str, Any]:
    """Return actual songs by a specific artist -- a direct catalog pull, not
    a similarity recommendation like recommend_from_song/recommend_from_playlist.

    Resolves `artist` (a name) to its YouTube Music channel and pulls its
    real song catalog, excluding anything already in Liked Music OR in ANY
    of the user's playlists (not just one, unlike recommend_from_playlist's
    single-seed-playlist exclusion). Read-only: never adds results anywhere.

    This is a hard requirement, not best-effort -- if fewer than `limit`
    qualifying songs exist after exclusion, this returns however many were
    actually found rather than padding the list. Check `found` vs
    `requested` in the result to see whether it fell short.

    The library exclusion set is cached for speed; newly liked songs are
    always honoured, but call refresh_library() after adding songs to a
    playlist by other means.
    """
    yt = _client()
    resolved = _resolve_artist(yt, artist)
    if resolved is None or not resolved.get("browseId"):
        return {"artist": None, "requested": limit, "found": 0, "songs": []}

    catalog = _artist_song_catalog(yt, resolved["browseId"])
    exclude = _library_video_ids(yt)

    songs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in catalog:
        track = _norm_track(item)
        vid = track["videoId"]
        if not vid or vid in exclude or vid in seen:
            continue
        seen.add(vid)
        songs.append(track)
        if len(songs) >= limit:
            break

    return {
        "artist": resolved.get("artist"),
        "requested": limit,
        "found": len(songs),
        "songs": songs,
    }


@mcp.tool()
@handle_errors
def refresh_library() -> dict[str, Any]:
    """Rebuild the cached library exclusion set from scratch, right now.

    Every recommendation tool excludes songs already in Liked Music or any of
    your playlists. That set is expensive to build (~20s), so it's cached and
    reused. Liking a song is picked up immediately regardless, but adding a
    song to some other playlist is only seen once the cache is rebuilt.

    Call this after adding songs to a playlist by other means (e.g. a
    playlist-management tool) if you want the next recommendation to account
    for them without waiting out the cache TTL.
    """
    yt = _client()
    ids = _library_video_ids(yt, force_refresh=True)
    return {
        "tracks_excluded": len(ids),
        "cache_path": str(CACHE_PATH),
        "ttl_seconds": CACHE_TTL,
    }


# --- v2: mood ---------------------------------------------------------------


@mcp.tool()
@handle_errors
def recommend_for_mood(
    feeling: str | None = None,
    vector: dict[str, float] | None = None,
    context: str | None = None,
    arc: str = "mirror",
    limit: int = 20,
    genres: list[str] | None = None,
) -> dict[str, Any]:
    """Recommend new songs that match how the listener actually feels right now.

    Unlike recommend_from_song, the mood decides where candidates come from:
    seeds are drawn from the listener's OWN library nearest the target mood,
    then expanded through radio/related/artist signals and songs from YouTube's
    mood playlists. Results are still guaranteed absent from their library.

    Describing the mood -- in priority order:
      `vector`  The precise path, and the one to prefer. A dict with
                valence (-1..1, despairing->euphoric), energy (0..1,
                still->frantic), tension (0..1, resolved->anxious; this is what
                separates angry from excited) and depth (0..1, background
                ->lyric-forward). YOU should read the user's words and set
                these -- you understand "wistful but still wants to get things
                done" far better than any keyword list.
      `feeling` Their words verbatim, as a fallback when you'd rather not
                commit to numbers. Matched against a mood-word lexicon.
      `context` One of: Chill, Sleep, Focus, Commute, Feel good, Romance,
                Energize, Workout, Party, Gaming, Sad.
      If none are given, the mood is inferred from recent listening history.

    `arc` shapes the sequence rather than returning a flat mood-matched set:
      mirror  stay where they are and validate it (default)
      lift    start where they are, rise gradually -- never jump straight to
              upbeat when someone is low, it reads as being told to cheer up
      settle  descend to calm; an evening wind-down
      deepen  go further in; sometimes you want to sit in it properly
      hold    stay in a band with energy as a curve (workout: warmup/peak/cooldown)

    `genres` optionally restricts the seeds to the listener's own genre
    playlists, e.g. ["Punjabi", "Hip-Hop & Rap"].

    The result carries `target` (the mood aimed at), `target_origin` (where it
    came from), `seeds` (which of their songs it grew from), `notes` (caveats
    worth repeating to the user) and `songs`, each with its slot, mood fit and
    which signals surfaced it.
    """
    import recommend
    import store as _s

    yt = _client()
    conn = _store()
    exclude = _library_video_ids(yt) | _s.rejected_video_ids(conn)

    result = recommend.build(
        yt, conn, exclude=exclude, feeling=feeling, vector=vector,
        context=context, arc=arc, limit=limit, genres=genres,
    )
    _s.log_recommendations(conn, result["songs"], result["target"], feeling, arc)
    return result


@mcp.tool()
@handle_errors
def read_my_mood() -> dict[str, Any]:
    """Infer the listener's current mood from recent listening, with evidence.

    Returns the inferred `vector`, a plain-language `described`, a `confidence`,
    and `evidence` -- the specific observations behind it (a song on repeat, one
    artist dominating, valence drifting across the session).

    Lead with the evidence, not the verdict. "You've had these three on loop
    since yesterday -- want something that sits there with you, or something
    that lifts?" is the point of this tool; asserting "you are sad" is not.
    Mood inference is often wrong, so offer it as a read the user can correct.
    """
    import moodspace
    import sense

    read = sense.read_mood(_store(), _client())
    return {
        **read,
        "described": moodspace.describe(read["vector"]) if read["vector"] else None,
    }


@mcp.tool()
@handle_errors
def explain_recommendation(video_id: str) -> dict[str, Any]:
    """Explain why a song was recommended, in mood terms.

    Reports the song's mood vector, which layer produced it (Claude reading the
    lyrics, YouTube mood-playlist membership, or the artist's own average), the
    named moods it sits closest to, and the mood it was last served against.
    """
    import label
    import moodspace
    import store as _s

    conn = _store()
    track = _s.get_track(conn, video_id) or {}
    entry = label.resolve(conn, video_id)

    served = conn.execute(
        "SELECT served_at, feeling, arc, slot, valence, energy, tension, depth "
        "FROM recommendation WHERE video_id = ? ORDER BY served_at DESC LIMIT 1",
        (video_id,),
    ).fetchone()

    return {
        "videoId": video_id,
        "title": track.get("title"),
        "artists": track.get("artists"),
        "mood": entry["vector"] if entry else None,
        "mood_source": entry["source"] if entry else None,
        "confidence": entry["confidence"] if entry else None,
        "described": moodspace.describe(entry["vector"]) if entry else None,
        "closest_moods": [name for name, _ in moodspace.nearest_anchors(entry["vector"], 3)] if entry else [],
        "atlas_playlists": _s.atlas_moods_for(conn, video_id)[:8],
        "genre": label.genre_prior(conn, video_id),
        "last_served_against": dict(served) if served else None,
    }


@mcp.tool()
@handle_errors
def record_feedback(video_id: str, reaction: str) -> dict[str, Any]:
    """Record what the listener thought of a recommendation.

    `reaction` is one of: loved, saved, skipped, wrong_mood.

    `wrong_mood` is the valuable one -- it says the song was fine but the mood
    read was off, which is a different failure from simply not liking it.
    Anything marked skipped or wrong_mood is never recommended again.
    """
    import store as _s

    allowed = {"loved", "saved", "skipped", "wrong_mood"}
    if reaction not in allowed:
        raise RuntimeError(f"reaction must be one of: {', '.join(sorted(allowed))}.")

    _s.put_feedback(_store(), video_id, reaction)
    return {"videoId": video_id, "reaction": reaction, "recorded": True}


@mcp.tool()
@handle_errors
def index_status() -> dict[str, Any]:
    """Report how much of the mood index exists, so gaps are visible not silent.

    Covers the mood-playlist crawl, how much of the listener's library carries a
    mood label and from which layer, and whether Claude-based labelling is
    configured. Low coverage means recommendations are ranking mostly on signal
    agreement rather than on mood -- worth saying out loud.
    """
    import judge
    import label
    import store as _s

    conn = _store()
    return {
        "atlas": _s.atlas_stats(conn),
        "library": label.library_coverage(conn),
        "llm_labelling": {
            "available": judge.available(),
            "model": judge.MODEL,
            "hint": None if judge.available() else "Install with: pip install -e '.[llm]' and run 'ant auth login'.",
        },
    }


if __name__ == "__main__":
    mcp.run()
