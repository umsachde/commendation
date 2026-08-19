"""Unit tests for server.py.

Everything here runs against a hand-rolled fake YTMusic client -- no network,
no headers_auth.json required. This complements (does not replace)
scripts/test_recommend.py, which is a real-account smoke test.
"""

import json

import pytest
import requests
from ytmusicapi.exceptions import (
    YTMusicError,
    YTMusicGatedError,
    YTMusicServerError,
    YTMusicUserError,
)

import server
from server import (
    AUTH_HELP,
    AUTH_PATH,
    _finalize,
    _gather_seed_candidates,
    _liked_video_ids,
    _merge_and_score,
    _norm_track,
    handle_errors,
    recommend_from_playlist,
    recommend_from_song,
)


# --- _norm_track -------------------------------------------------------


def test_norm_track_artists_list():
    item = {"videoId": "v1", "title": "Song", "artists": [{"name": "A"}, {"name": "B"}]}
    assert _norm_track(item)["artists"] == ["A", "B"]


def test_norm_track_artists_missing_name_is_dropped():
    item = {"videoId": "v1", "title": "Song", "artists": [{"name": "A"}, {"id": "x"}]}
    assert _norm_track(item)["artists"] == ["A"]


def test_norm_track_falls_back_to_artist_string():
    item = {"videoId": "v1", "title": "Song", "artist": "Solo Artist"}
    assert _norm_track(item)["artists"] == ["Solo Artist"]


def test_norm_track_no_artist_info():
    item = {"videoId": "v1", "title": "Song"}
    assert _norm_track(item)["artists"] == []


def test_norm_track_album_dict():
    item = {"videoId": "v1", "title": "Song", "album": {"name": "Album Name", "id": "al1"}}
    assert _norm_track(item)["album"] == "Album Name"


def test_norm_track_album_string():
    item = {"videoId": "v1", "title": "Song", "album": "Album Name"}
    assert _norm_track(item)["album"] == "Album Name"


def test_norm_track_album_missing():
    item = {"videoId": "v1", "title": "Song"}
    assert _norm_track(item)["album"] is None


# --- _merge_and_score ----------------------------------------------------


def test_merge_and_score_single_seed():
    per_seed = [
        {
            "v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"radio", "related"}},
        }
    ]
    merged = _merge_and_score(per_seed)
    assert merged["v1"]["score"] == 2
    assert merged["v1"]["sources"] == {"radio", "related"}


def test_merge_and_score_combines_across_seeds():
    per_seed = [
        {"v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"radio", "related"}}},
        {"v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"artist"}}},
    ]
    merged = _merge_and_score(per_seed)
    assert merged["v1"]["score"] == 3
    assert merged["v1"]["sources"] == {"radio", "related", "artist"}


def test_merge_and_score_keeps_candidates_separate():
    per_seed = [
        {"v1": {"videoId": "v1", "title": "T1", "artists": [], "album": None, "sources": {"radio"}}},
        {"v2": {"videoId": "v2", "title": "T2", "artists": [], "album": None, "sources": {"radio"}}},
    ]
    merged = _merge_and_score(per_seed)
    assert set(merged.keys()) == {"v1", "v2"}


# --- _finalize -------------------------------------------------------------


def _candidate(vid, score, title="T", sources=("radio",)):
    return {"videoId": vid, "title": title, "artists": [], "album": None, "score": score, "sources": set(sources)}


def test_finalize_excludes_ids():
    merged = {"v1": _candidate("v1", 1), "v2": _candidate("v2", 2)}
    out = _finalize(merged, exclude={"v1"}, limit=10)
    assert [c["videoId"] for c in out] == ["v2"]


def test_finalize_sorts_by_score_desc_then_title_asc():
    merged = {
        "a": _candidate("a", 1, title="Zebra"),
        "b": _candidate("b", 3, title="Apple"),
        "c": _candidate("c", 3, title="Banana"),
    }
    out = _finalize(merged, exclude=set(), limit=10)
    assert [c["videoId"] for c in out] == ["b", "c", "a"]


def test_finalize_respects_limit():
    merged = {str(i): _candidate(str(i), i) for i in range(5)}
    out = _finalize(merged, exclude=set(), limit=2)
    assert len(out) == 2
    assert [c["videoId"] for c in out] == ["4", "3"]


def test_finalize_sources_sorted_in_output():
    merged = {"v1": _candidate("v1", 1, sources=("related", "artist", "radio"))}
    out = _finalize(merged, exclude=set(), limit=10)
    assert out[0]["sources"] == ["artist", "radio", "related"]


# --- _liked_video_ids --------------------------------------------------


class _FakeYT:
    def __init__(self, watch=None, related_sections=None, artists=None, playlists=None):
        self._watch = watch or {}
        self._related_sections = related_sections or {}
        self._artists = artists or {}
        self._playlists = playlists or {}
        self.get_song_related_calls = []
        self.get_artist_calls = []

    def get_watch_playlist(self, videoId, limit=25, radio=True):
        result = self._watch.get(videoId)
        if isinstance(result, Exception):
            raise result
        return result

    def get_song_related(self, browse_id):
        self.get_song_related_calls.append(browse_id)
        result = self._related_sections.get(browse_id)
        if isinstance(result, Exception):
            raise result
        return result or []

    def get_artist(self, artist_id):
        self.get_artist_calls.append(artist_id)
        result = self._artists.get(artist_id)
        if isinstance(result, Exception):
            raise result
        return result

    def get_playlist(self, playlist_id, limit=None):
        return self._playlists[playlist_id]


def test_liked_video_ids_filters_missing_ids():
    yt = _FakeYT(playlists={"LM": {"tracks": [{"videoId": "a"}, {"videoId": "b"}, {"videoId": None}, {}]}})
    assert _liked_video_ids(yt) == {"a", "b"}


# --- _gather_seed_candidates --------------------------------------------


def test_gather_seed_candidates_full_pipeline():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed Song", "artists": [{"name": "SeedArtist", "id": "artist1"}]},
                {"videoId": "radio1", "title": "Radio Song", "artists": [{"name": "X"}]},
            ],
            "related": "REL_BROWSE",
        }
    }
    related_sections = {
        "REL_BROWSE": [
            {
                "contents": [
                    {"videoId": "related1", "title": "Related Song", "artists": [{"name": "Y"}]},
                    "some artist bio text",  # regression: non-dict items must not crash parsing
                    {"videoId": seed, "title": "Seed Song"},  # seed reappearing must be excluded
                ]
            }
        ]
    }
    artists = {
        "artist1": {
            "songs": {"results": [{"videoId": "artistsong1", "title": "Artist Song", "artists": [{"name": "SeedArtist"}]}]},
            "related": {
                "results": [
                    {"browseId": "relartist1"},
                    {"browseId": "relartist2"},
                    {"browseId": "relartist3"},  # beyond _RELATED_ARTISTS_TO_EXPAND=2, should be ignored
                ]
            },
        },
        "relartist1": {"songs": {"results": [{"videoId": "relsong1", "title": "Rel Artist Song 1"}]}},
        "relartist2": {"songs": {"results": [{"videoId": "relsong2", "title": "Rel Artist Song 2"}]}},
    }
    yt = _FakeYT(watch=watch, related_sections=related_sections, artists=artists)

    found = _gather_seed_candidates(yt, seed)

    assert seed not in found
    assert found["radio1"]["sources"] == {"radio"}
    assert found["related1"]["sources"] == {"related"}
    assert found["artistsong1"]["sources"] == {"artist"}
    assert found["relsong1"]["sources"] == {"artist"}
    assert found["relsong2"]["sources"] == {"artist"}
    assert "relartist3" not in yt.get_artist_calls  # only first 2 related artists expanded


def test_gather_seed_candidates_signal_failure_is_skipped_not_fatal():
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [{"videoId": seed, "title": "Seed", "artists": [{"name": "A", "id": "artist1"}]}],
            "related": "REL_BROWSE",
        }
    }
    related_sections = {"REL_BROWSE": YTMusicError("related signal down")}
    artists = {"artist1": {"songs": {"results": [{"videoId": "artistsong1", "title": "Artist Song"}]}, "related": {"results": []}}}
    yt = _FakeYT(watch=watch, related_sections=related_sections, artists=artists)

    found = _gather_seed_candidates(yt, seed)

    assert "artistsong1" in found  # artist expansion still worked despite related failing
    assert all("related" not in c["sources"] for c in found.values())


def test_gather_seed_candidates_total_watch_failure_returns_empty():
    seed = "seed1"
    yt = _FakeYT(watch={seed: requests.exceptions.RequestException("network down")})
    assert _gather_seed_candidates(yt, seed) == {}


def test_gather_seed_candidates_excludes_seed_from_radio_tracks():
    seed = "seed1"
    watch = {seed: {"tracks": [{"videoId": seed, "title": "Seed"}], "related": None}}
    yt = _FakeYT(watch=watch)
    assert _gather_seed_candidates(yt, seed) == {}


# --- handle_errors -------------------------------------------------------


def test_handle_errors_passes_through_success():
    @handle_errors
    def fn():
        return 42

    assert fn() == 42


def test_handle_errors_file_not_found():
    @handle_errors
    def fn():
        raise FileNotFoundError()

    with pytest.raises(RuntimeError, match="setup_auth_from_file.py"):
        fn()
    with pytest.raises(RuntimeError, match=AUTH_PATH.replace(".", r"\.")):
        fn()


def test_handle_errors_json_decode_error():
    @handle_errors
    def fn():
        raise json.JSONDecodeError("bad", "doc", 0)

    with pytest.raises(RuntimeError, match="unexpected response"):
        fn()


def test_handle_errors_server_error_401_maps_to_auth_help():
    @handle_errors
    def fn():
        raise YTMusicServerError("HTTP 401 Unauthorized")

    with pytest.raises(RuntimeError, match=AUTH_HELP.split(".")[0]):
        fn()


def test_handle_errors_server_error_403_maps_to_auth_help():
    @handle_errors
    def fn():
        raise YTMusicServerError("HTTP 403 Forbidden")

    with pytest.raises(RuntimeError, match=AUTH_HELP.split(".")[0]):
        fn()


def test_handle_errors_server_error_429_maps_to_rate_limit_message():
    @handle_errors
    def fn():
        raise YTMusicServerError("HTTP 429 Too Many Requests")

    with pytest.raises(RuntimeError, match="rate-limiting"):
        fn()


def test_handle_errors_other_server_error():
    @handle_errors
    def fn():
        raise YTMusicServerError("HTTP 500 Internal Server Error")

    with pytest.raises(RuntimeError, match="YouTube Music server error"):
        fn()


def test_handle_errors_gated_error():
    @handle_errors
    def fn():
        raise YTMusicGatedError("interaction required")

    with pytest.raises(RuntimeError, match="gated/restricted"):
        fn()


def test_handle_errors_user_error():
    @handle_errors
    def fn():
        raise YTMusicUserError("bad usage")

    with pytest.raises(RuntimeError, match="bad usage"):
        fn()


def test_handle_errors_generic_ytmusic_error():
    @handle_errors
    def fn():
        raise YTMusicError("something else")

    with pytest.raises(RuntimeError, match="YouTube Music error"):
        fn()


def test_handle_errors_network_error():
    @handle_errors
    def fn():
        raise requests.exceptions.ConnectionError("no route to host")

    with pytest.raises(RuntimeError, match="Network error"):
        fn()


# --- recommend_from_song / recommend_from_playlist (integration) --------


def test_recommend_from_song_excludes_liked(monkeypatch):
    seed = "seed1"
    watch = {
        seed: {
            "tracks": [
                {"videoId": seed, "title": "Seed"},
                {"videoId": "cand1", "title": "Candidate 1", "artists": [{"name": "A"}]},
                {"videoId": "liked1", "title": "Already Liked", "artists": [{"name": "B"}]},
            ],
            "related": None,
        }
    }
    yt = _FakeYT(watch=watch, playlists={"LM": {"tracks": [{"videoId": "liked1"}]}})
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_song(seed, limit=20)

    ids = [r["videoId"] for r in results]
    assert "cand1" in ids
    assert "liked1" not in ids
    assert seed not in ids


def test_recommend_from_playlist_excludes_seed_playlist_and_liked(monkeypatch):
    tracks = [{"videoId": "t1"}, {"videoId": "t2"}]
    watch = {
        "t1": {"tracks": [{"videoId": "t1"}, {"videoId": "cand1", "title": "C1", "artists": []}], "related": None},
        "t2": {"tracks": [{"videoId": "t2"}, {"videoId": "t1", "title": "T1 again"}], "related": None},
    }
    yt = _FakeYT(
        watch=watch,
        playlists={"PL1": {"tracks": tracks}, "LM": {"tracks": [{"videoId": "liked1"}]}},
    )
    monkeypatch.setattr(server, "_client", lambda: yt)

    results = recommend_from_playlist("PL1", limit=20, seed_sample_size=5)

    ids = {r["videoId"] for r in results}
    assert ids == {"cand1"}  # t1/t2 excluded as seed-playlist members, seeds sampled == full playlist here


def test_recommend_from_playlist_empty_playlist_returns_empty(monkeypatch):
    yt = _FakeYT(playlists={"PL1": {"tracks": []}, "LM": {"tracks": []}})
    monkeypatch.setattr(server, "_client", lambda: yt)

    assert recommend_from_playlist("PL1") == []


def test_recommend_from_playlist_samples_when_over_seed_size(monkeypatch):
    tracks = [{"videoId": f"t{i}"} for i in range(10)]
    watch = {f"t{i}": {"tracks": [{"videoId": f"t{i}"}], "related": None} for i in range(10)}
    yt = _FakeYT(watch=watch, playlists={"PL1": {"tracks": tracks}, "LM": {"tracks": []}})
    monkeypatch.setattr(server, "_client", lambda: yt)
    monkeypatch.setattr(server.random, "sample", lambda population, k: population[:k])

    recommend_from_playlist("PL1", seed_sample_size=3)

    # only the first 3 tracks should have been used as seeds
    seeded_ids = {t["videoId"] for t in tracks[:3]}
    assert seeded_ids == {"t0", "t1", "t2"}
