"""MCP server that recommends new YouTube Music songs.

Combines multiple independent YouTube Music signals (radio, related content,
artist catalog expansion) instead of trusting a single algorithm, and always
excludes anything already in the user's Liked Music.
"""

import functools
import json
import os
import random
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

mcp = MCPServer("commendation")

_yt: YTMusic | None = None


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

    return wrapper


# --- candidate generation ---------------------------------------------------

_SOURCE_RADIO = "radio"
_SOURCE_RELATED = "related"
_SOURCE_ARTIST = "artist"
_RELATED_ARTISTS_TO_EXPAND = 2  # how many of the seed artist's related artists to also pull top songs from

_SIGNAL_ERRORS = (YTMusicError, requests.exceptions.RequestException)


def _norm_track(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize the differing track shapes returned by watch playlists,
    related content, and artist song lists into one consistent record."""
    artists = item.get("artists")
    if artists:
        names = [a.get("name") for a in artists if a.get("name")]
    elif item.get("artist"):
        names = [item["artist"]]
    else:
        names = []

    album = item.get("album")
    if isinstance(album, dict):
        album_name = album.get("name")
    elif isinstance(album, str):
        album_name = album
    else:
        album_name = None

    return {
        "videoId": item.get("videoId"),
        "title": item.get("title"),
        "artists": names,
        "album": album_name,
    }


def _gather_seed_candidates(yt: YTMusic, seed_video_id: str) -> dict[str, dict[str, Any]]:
    """Pull radio + related + artist-expansion candidates for one seed song.

    Returns videoId -> {normalized track fields..., "sources": set[str]}.
    A signal that fails is skipped rather than aborting the whole seed.
    """
    found: dict[str, dict[str, Any]] = {}

    def add(item: dict[str, Any], source: str) -> None:
        vid = item.get("videoId")
        if not vid or vid == seed_video_id:
            return
        if vid not in found:
            found[vid] = {**_norm_track(item), "sources": set()}
        found[vid]["sources"].add(source)

    watch = None
    try:
        watch = yt.get_watch_playlist(videoId=seed_video_id, limit=25, radio=True)
    except _SIGNAL_ERRORS:
        pass

    seed_artist_id = None
    if watch:
        for t in watch.get("tracks", []):
            add(t, _SOURCE_RADIO)
            if t.get("videoId") == seed_video_id and t.get("artists"):
                seed_artist_id = t["artists"][0].get("id")

        related_browse_id = watch.get("related")
        if related_browse_id:
            try:
                sections = yt.get_song_related(related_browse_id)
            except _SIGNAL_ERRORS:
                sections = []
            for section in sections:
                for item in section.get("contents") or []:
                    if isinstance(item, dict) and item.get("videoId"):
                        add(item, _SOURCE_RELATED)

    if seed_artist_id:
        artist = None
        try:
            artist = yt.get_artist(seed_artist_id)
        except _SIGNAL_ERRORS:
            pass
        if artist:
            for s in (artist.get("songs") or {}).get("results", []):
                add(s, _SOURCE_ARTIST)
            related_artists = (artist.get("related") or {}).get("results", [])
            for rel in related_artists[:_RELATED_ARTISTS_TO_EXPAND]:
                rel_id = rel.get("browseId")
                if not rel_id:
                    continue
                try:
                    rel_artist = yt.get_artist(rel_id)
                except _SIGNAL_ERRORS:
                    continue
                for s in (rel_artist.get("songs") or {}).get("results", []):
                    add(s, _SOURCE_ARTIST)

    return found


def _merge_and_score(per_seed: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Combine candidates from multiple seeds. Score = number of distinct
    (seed, source) pairs that surfaced each candidate."""
    merged: dict[str, dict[str, Any]] = {}
    for found in per_seed:
        for vid, data in found.items():
            entry = merged.get(vid)
            if entry is None:
                entry = {
                    "videoId": data["videoId"],
                    "title": data["title"],
                    "artists": data["artists"],
                    "album": data["album"],
                    "sources": set(),
                    "score": 0,
                }
                merged[vid] = entry
            entry["sources"] |= data["sources"]
            entry["score"] += len(data["sources"])
    return merged


def _finalize(merged: dict[str, dict[str, Any]], exclude: set[str], limit: int) -> list[dict[str, Any]]:
    ranked = [c for vid, c in merged.items() if vid not in exclude]
    ranked.sort(key=lambda c: (-c["score"], c.get("title") or ""))
    return [
        {
            "videoId": c["videoId"],
            "title": c["title"],
            "artists": c["artists"],
            "album": c["album"],
            "score": c["score"],
            "sources": sorted(c["sources"]),
        }
        for c in ranked[:limit]
    ]


def _liked_video_ids(yt: YTMusic) -> set[str]:
    # get_liked_songs() defaults to only the most recent 100 items -- not
    # enough for a real library. get_playlist("LM", limit=None) fetches all.
    liked = yt.get_playlist("LM", limit=None)
    return {t["videoId"] for t in liked.get("tracks", []) if t.get("videoId")}


# --- tools -------------------------------------------------------------


@mcp.tool()
@handle_errors
def recommend_from_song(video_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recommend new songs similar to a seed song.

    Combines YouTube Music's radio, its separate "related" signal, and the
    seed artist's own catalog plus related artists' catalogs, then ranks by
    how many independent signals agreed on each candidate. Never returns a
    song already in Liked Music.
    """
    yt = _client()
    candidates = _gather_seed_candidates(yt, video_id)
    merged = _merge_and_score([candidates])
    exclude = _liked_video_ids(yt)
    return _finalize(merged, exclude, limit)


@mcp.tool()
@handle_errors
def recommend_from_playlist(playlist_id: str, limit: int = 20, seed_sample_size: int = 5) -> list[dict[str, Any]]:
    """Recommend new songs based on an entire playlist.

    Randomly samples up to seed_sample_size tracks from the playlist as seeds
    (the whole playlist if it's smaller), runs the same multi-signal
    candidate generation as recommend_from_song for each, and pools/ranks the
    results. Never returns a song already in Liked Music OR already in the
    source playlist.
    """
    yt = _client()
    playlist = yt.get_playlist(playlist_id, limit=None)
    tracks = [t for t in playlist.get("tracks", []) if t.get("videoId")]
    if not tracks:
        return []

    sample = tracks if len(tracks) <= seed_sample_size else random.sample(tracks, seed_sample_size)

    per_seed = [_gather_seed_candidates(yt, t["videoId"]) for t in sample]
    merged = _merge_and_score(per_seed)

    exclude = _liked_video_ids(yt) | {t["videoId"] for t in tracks}
    return _finalize(merged, exclude, limit)


if __name__ == "__main__":
    mcp.run()
