#!/usr/bin/env python3
"""Record a timestamped snapshot of recent listening.

get_history() reports only "Today" or "Yesterday" -- order, never a clock time.
Stamping snapshots locally is the only way this system ever learns that someone
was listening at 2am, or that one song has been on repeat for three days.

Worth running on a schedule, e.g. every few hours:

    0 */3 * * * cd /path/to/commendation && .venv/bin/python scripts/snapshot_history.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sense  # noqa: E402
import store  # noqa: E402
from ytmusicapi import YTMusic  # noqa: E402


def main() -> int:
    auth = os.environ.get("COMMENDATION_AUTH_PATH", "headers_auth.json")
    if not Path(auth).exists():
        print(f"error: {auth} not found. Run scripts/setup_auth_from_file.py first.", file=sys.stderr)
        return 1

    conn = store.connect()
    try:
        recorded = sense.snapshot(conn, YTMusic(auth))
    except Exception as e:  # noqa: BLE001 - a cron job must fail loudly, not silently
        print(f"error: history snapshot failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    total = conn.execute("SELECT COUNT(DISTINCT video_id) AS n FROM history_log").fetchone()["n"]
    print(f"recorded {recorded} plays; {total:,} distinct tracks in the history log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
