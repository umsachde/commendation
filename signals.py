"""Candidate generation: the multi-signal discovery core.

Extracted from server.py so that both v1's similarity tools and v2's mood
recommendations draw candidates the same way. The ranking on top differs; the
way songs are found does not.

Three independent signals per seed -- YouTube's radio, its separate "related"
feed, and the seed artist's own catalogue plus adjacent artists. A candidate's
score is how many distinct (seed, signal) pairs surfaced it, which makes
agreement between independent signals the thing that ranks, rather than trust
in any one algorithm.
"""

from typing import Any

import requests
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicError

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


def _gather_seed_candidates(
    yt: YTMusic, seed_video_id: str, seed_artist_names: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Pull radio + related + artist-expansion candidates for one seed song.

    Returns videoId -> {normalized track fields..., "sources": set[str]}.
    A signal that fails is skipped rather than aborting the whole seed.

    If `seed_artist_names` is passed, the seed track's own artist name(s) are
    appended to it as a side effect -- lets callers recover the seed's artist
    without a second lookup, e.g. to filter candidates down to that artist.
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
                if seed_artist_names is not None:
                    seed_artist_names.extend(a.get("name") for a in t["artists"] if a.get("name"))

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


def _artist_names_match(candidate_artists: list[str], target_names_lower: list[str]) -> bool:
    """Loose match (case-insensitive substring, either direction) between a
    candidate's artist credits and a set of target names -- mirrors the
    matching already used in _resolve_song_video_id, since search/catalog
    artist-name formatting varies (features, "&" vs "feat.", etc)."""
    for name in candidate_artists:
        if not name:
            continue
        name_lower = name.lower()
        if any(t in name_lower or name_lower in t for t in target_names_lower):
            return True
    return False


def _filter_same_artist(merged: dict[str, dict[str, Any]], target_names: list[str]) -> dict[str, dict[str, Any]]:
    """Restrict candidates to those crediting one of target_names. No-op if
    target_names is empty (nothing to filter by)."""
    targets_lower = [t.lower() for t in target_names if t]
    if not targets_lower:
        return merged
    return {vid: c for vid, c in merged.items() if _artist_names_match(c["artists"], targets_lower)}


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


