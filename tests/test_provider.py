"""Unit tests for the provider seam: RECOM_PROVIDER selection in server._client(),
and the shared ProviderError hierarchy both backends' error types plug into.
"""

import pytest

import server
from provider import ProviderError
from spotify_client import SpotifyMCPError
from ytmusic_client import YTMusicMCPError


def test_ytmusic_and_spotify_errors_are_provider_errors():
    assert isinstance(YTMusicMCPError("x"), ProviderError)
    assert isinstance(SpotifyMCPError("x"), ProviderError)


def test_client_defaults_to_youtube(monkeypatch):
    monkeypatch.setattr(server, "PROVIDER", "youtube")
    monkeypatch.setattr(server, "_yt", None)
    monkeypatch.setattr(server, "YTMusicClient", lambda: "fake-ytmusic-client")
    assert server._client() == "fake-ytmusic-client"


def test_client_selects_spotify(monkeypatch):
    monkeypatch.setattr(server, "PROVIDER", "spotify")
    monkeypatch.setattr(server, "_yt", None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "spotify_client",
        type("_M", (), {"SpotifyClient": lambda: "fake-spotify-client"}),
    )
    assert server._client() == "fake-spotify-client"


def test_client_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(server, "PROVIDER", "deezer")
    monkeypatch.setattr(server, "_yt", None)
    with pytest.raises(RuntimeError, match="Unknown RECOM_PROVIDER"):
        server._client()


def test_client_reuses_cached_instance(monkeypatch):
    monkeypatch.setattr(server, "PROVIDER", "youtube")
    monkeypatch.setattr(server, "_yt", "already-connected")
    monkeypatch.setattr(server, "YTMusicClient", lambda: pytest.fail("should not reconnect"))
    assert server._client() == "already-connected"
