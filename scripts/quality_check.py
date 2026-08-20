#!/usr/bin/env python3
"""Measure recommendation quality, so "better" is a number rather than a hunch.

Runs a fixed set of mood/arc cases and reports:

  mean fit           how well picks match the mood they were asked for
  cross-mood overlap how much different moods return the SAME songs
  distinct songs     total unique songs across every case
  rated              what fraction of picks carry a real mood label
  artists/10         variety within a single result

Cross-mood overlap is the important one and the reason this script exists. A
run once scored a healthy 0.775 mean fit while "heartbroken" and "angry"
returned 70% the same songs -- the engine had only 44 distinct songs to offer
across 8 moods. Fit alone cannot see that; overlap can.

    python scripts/quality_check.py                 # measure current behaviour
    python scripts/quality_check.py --label run-name --json out.json
    python scripts/quality_check.py --distinctiveness 0   # A/B the seed scoring
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import label as label_mod  # noqa: E402
import moodspace  # noqa: E402
import recommend  # noqa: E402
import store  # noqa: E402
from ytmusicapi import YTMusic  # noqa: E402

CASES = [
    ("heartbroken and low", None, "mirror"),
    ("heartbroken and low", None, "lift"),
    ("need to focus and get work done", None, "mirror"),
    (None, "Workout", "hold"),
    ("chill evening winding down", None, "settle"),
    ("angry", None, "mirror"),
    ("nostalgic", None, "mirror"),
    (None, "Party", "mirror"),
]


def measure(yt, conn, limit: int = 10) -> dict:
    exclude = store.library_video_ids(conn) | store.rejected_video_ids(conn)
    rows = []
    for feeling, context, arc in CASES:
        started = time.time()
        result = recommend.build(yt, conn, exclude=exclude, feeling=feeling,
                                 context=context, arc=arc, limit=limit)
        songs = result["songs"]
        fits = [s["mood_fit"] for s in songs if s["mood_fit"] is not None]
        rows.append({
            "case": f"{feeling or context}/{arc}",
            "n": len(songs),
            "rated": sum(1 for s in songs if s["rated"]),
            "mean_fit": round(statistics.mean(fits), 3) if fits else None,
            "min_fit": round(min(fits), 3) if fits else None,
            "artists": len({(s["artists"] or ["?"])[0] for s in songs}),
            "seconds": round(time.time() - started, 1),
            "titles": [f"{s['title']} — {', '.join(s['artists'])}" for s in songs],
        })

    sets = {r["case"]: set(r["titles"]) for r in rows}
    names = [n for n in sets if sets[n]]
    pairs = sorted(
        ((round(len(sets[a] & sets[b]) / max(len(sets[a]), 1), 2), a, b) for i, a in enumerate(names) for b in names[i + 1:]),
        reverse=True,
    )
    fits = [r["mean_fit"] for r in rows if r["mean_fit"] is not None]

    return {
        "library_coverage": label_mod.library_coverage(conn),
        "atlas": {k: v for k, v in store.atlas_stats(conn).items() if k != "moods"},
        "mean_fit": round(statistics.mean(fits), 3) if fits else None,
        "cross_mood_overlap": round(statistics.mean(p[0] for p in pairs), 3) if pairs else None,
        "distinct_songs": len(set().union(*sets.values())) if sets else 0,
        "total_slots": sum(len(v) for v in sets.values()),
        "rated_fraction": round(sum(r["rated"] for r in rows) / max(sum(r["n"] for r in rows), 1), 3),
        "mean_artists": round(statistics.mean(r["artists"] for r in rows), 2),
        "worst_overlaps": pairs[:4],
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="run", help="name for this run")
    parser.add_argument("--json", type=Path, default=None, help="also write full results here")
    parser.add_argument("--limit", type=int, default=10, help="songs per case")
    parser.add_argument("--distinctiveness", type=float, default=None,
                        help="override moodspace.DISTINCTIVENESS_WEIGHT (0 disables it) to A/B the seed scoring")
    parser.add_argument("--titles", action="store_true", help="print every pick")
    args = parser.parse_args()

    auth = os.environ.get("RECOM_AUTH_PATH", "headers_auth.json")
    if not Path(auth).exists():
        print(f"error: {auth} not found. Run scripts/setup_auth_from_file.py first.", file=sys.stderr)
        return 1

    if args.distinctiveness is not None:
        moodspace.DISTINCTIVENESS_WEIGHT = args.distinctiveness

    conn = store.connect()
    if not store.library_video_ids(conn):
        print("error: no library recorded. Run scripts/label_library.py first.", file=sys.stderr)
        return 1

    result = measure(YTMusic(auth), conn, limit=args.limit)
    result["label"] = args.label

    cov = result["library_coverage"]
    print(f"=== {args.label} ===")
    print(f"atlas {result['atlas']['playlists_ok']:,} listings / {result['atlas']['unique_tracks']:,} tracks "
          f"| library coverage {cov['coverage'] * 100:.1f}% {cov['by_source']}")
    print(f"mean fit {result['mean_fit']} | cross-mood overlap {result['cross_mood_overlap']} (lower is better)")
    print(f"{result['distinct_songs']} distinct songs across {result['total_slots']} slots "
          f"| rated {result['rated_fraction'] * 100:.0f}% | artists/10 {result['mean_artists']}")
    for share, a, b in result["worst_overlaps"]:
        print(f"    overlap {share:.0%}: {a}  vs  {b}")
    for row in result["cases"]:
        print(f"  {row['case']:38s} fit {str(row['mean_fit']):5s} (min {row['min_fit']}) "
              f"rated {row['rated']}/{row['n']} artists {row['artists']} {row['seconds']}s")
        if args.titles:
            for title in row["titles"]:
                print(f"       {title}")

    if args.json:
        args.json.write_text(json.dumps(result, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
