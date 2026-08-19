"""
Standalone smoke test: confirms headers_auth.json works and that
recommend_from_song produces sane, liked-excluded results, without
touching the MCP layer at all.

    python scripts/test_recommend.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytmusicapi import YTMusic

from server import _client, _liked_video_ids, recommend_from_song

SEED_QUERY = "Daft Punk One More Time"


def main() -> int:
    try:
        yt = YTMusic("headers_auth.json")
    except Exception as e:
        print(f"Failed to load headers_auth.json: {e}")
        print("Run scripts/setup_auth.py first.")
        return 1

    results = yt.search(SEED_QUERY, filter="songs", limit=1)
    if not results:
        print(f"Search for {SEED_QUERY!r} returned nothing -- can't run the smoke test.")
        return 1

    seed = results[0]
    print(f"Seed: {seed['title']} — {', '.join(a['name'] for a in seed.get('artists') or [])}")

    recs = recommend_from_song(seed["videoId"], limit=10)
    if not recs:
        print("No recommendations returned -- something's off.")
        return 1

    liked = _liked_video_ids(_client())
    leaked = [r for r in recs if r["videoId"] in liked]

    print(f"\nGot {len(recs)} recommendations:")
    for r in recs:
        artists = ", ".join(r["artists"])
        sources = ", ".join(r["sources"])
        print(f"  [{r['score']}] {r['title']} — {artists} ({sources})")

    if leaked:
        print(f"\nFAILED: {len(leaked)} recommendation(s) were already in Liked Music.")
        return 1

    print("\nOK: no recommendations overlap with Liked Music.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
