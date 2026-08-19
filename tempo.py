"""BPM, via Deezer's public API.

YouTube Music exposes no tempo data whatsoever, so this comes from a second
source. Deezer's public API needs no key, no auth and no attribution, and
returns a `bpm` field on track detail.

Measured coverage on this library, and it matters: roughly 42% overall, but
6/6 on Pop and 1/6 on Punjabi and Bollywood. Verified that the misses are
genuinely `bpm: 0` in Deezer's data rather than failed matching -- every test
track resolved to the correct song, and scanning deeper into search results
finds nothing. So tempo is a real signal on part of the catalogue and simply
absent on the rest.

Two consequences shape the design:

  - Unknown tempo must never silently exclude a song. A tempo filter narrows
    the songs it can judge and leaves the rest ranked on everything else,
    saying so, because the alternative is a filter that quietly deletes an
    entire language from the results.
  - Tempo is NOT propagated by artist, unlike mood. An artist's songs share a
    sensibility but not a BPM; propagating it would invent data.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

import store

API = "https://api.deezer.com"
USER_AGENT = "commendation/0.2 (+https://github.com/umsachde/commendation)"

# Deezer permits roughly 50 requests per 5 seconds. Stay well under it.
THROTTLE = 0.12
TIMEOUT = 15

# How many search hits to inspect before giving up on finding a tempo.
MAX_CANDIDATES = 4

STATUS_OK = "ok"
STATUS_NO_BPM = "no_bpm"      # matched the song; Deezer has no tempo for it
STATUS_NO_MATCH = "no_match"  # nothing on Deezer resembling this song


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _artist_matches(candidate: str, wanted: str) -> bool:
    """Loose credit match -- Deezer and YouTube format features differently."""
    if not wanted:
        return True
    a, b = candidate.lower(), wanted.lower()
    return a in b or b in a


def lookup(title: str, artist: str | None = None, sleep=time.sleep) -> tuple[float | None, str, int | None]:
    """Find a tempo for one song. Returns (bpm, status, deezer_id)."""
    if not title:
        return None, STATUS_NO_MATCH, None

    query = urllib.parse.quote(f"{title} {artist}".strip()[:180])
    try:
        results = _get(f"{API}/search?q={query}&limit={MAX_CANDIDATES}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None, STATUS_NO_MATCH, None

    hits = results.get("data") or []
    if not hits:
        return None, STATUS_NO_MATCH, None

    first_id = hits[0].get("id")
    for hit in hits:
        if artist and not _artist_matches((hit.get("artist") or {}).get("name", ""), artist):
            continue
        sleep(THROTTLE)
        try:
            detail = _get(f"{API}/track/{hit['id']}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        bpm = detail.get("bpm")
        if bpm:
            return float(bpm), STATUS_OK, hit["id"]

    return None, STATUS_NO_BPM, first_id


def get_or_fetch(conn: Any, video_id: str, title: str, artist: str | None, sleep=time.sleep) -> float | None:
    """Cached tempo, hitting Deezer only on a genuine first look.

    Negative results are cached too -- rediscovering that Deezer has no tempo
    for a song costs two requests every time otherwise.
    """
    cached = store.get_tempo(conn, video_id)
    if cached is not None:
        return cached["bpm"]

    bpm, status, deezer_id = lookup(title, artist, sleep=sleep)
    store.put_tempo(conn, video_id, bpm, status, deezer_id)
    return bpm


def relative_distance(a: float, b: float) -> float:
    """Tempo distance, tolerant of half- and double-time.

    A 170bpm drum-and-bass track and an 85bpm hip-hop track sit on the same
    pulse; treating them as maximally far apart is musically wrong. Compare
    against b, b/2 and b*2 and keep the closest reading.
    """
    if not a or not b:
        return 1.0
    best = min(abs(a - candidate) / max(a, candidate) for candidate in (b, b / 2, b * 2))
    return min(1.0, best)


def similarity(a: float | None, b: float | None) -> float | None:
    """0..1 tempo agreement, or None when either song's tempo is unknown.

    None is deliberately distinct from 0.0: "we don't know" must not be
    scored as "definitely wrong".
    """
    if not a or not b:
        return None
    return 1.0 - relative_distance(a, b)


def in_range(bpm: float | None, low: float | None, high: float | None) -> bool | None:
    """Whether a tempo falls in a range. None when unknown."""
    if not bpm:
        return None
    if low is not None and bpm < low:
        return False
    if high is not None and bpm > high:
        return False
    return True


def backfill(conn: Any, rows: Iterable[dict[str, Any]], sleep=time.sleep, on_progress=None) -> dict[str, int]:
    """Resolve tempo for many songs, skipping anything already attempted."""
    stats = {"resolved": 0, "no_bpm": 0, "no_match": 0, "cached": 0}
    for index, row in enumerate(rows, start=1):
        video_id = row["video_id"]
        if store.get_tempo(conn, video_id) is not None:
            stats["cached"] += 1
            continue
        bpm, status, deezer_id = lookup(row.get("title"), row.get("artists"), sleep=sleep)
        store.put_tempo(conn, video_id, bpm, status, deezer_id)
        stats["resolved" if status == STATUS_OK else status] += 1
        if on_progress:
            on_progress({**stats, "index": index, "title": row.get("title"), "bpm": bpm})
        sleep(THROTTLE)
    return stats
