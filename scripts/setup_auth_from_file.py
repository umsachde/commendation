"""
Non-interactive alternative to setup_auth.py.

Instead of pasting headers into a live input() prompt (which needs a real
EOF/Ctrl-D and doesn't survive well through some terminal bridges), paste
the raw request headers into a plain text file first, then run this.

1. Create ./raw_headers.txt in the project root (any text editor is fine)
   and paste the raw request headers you copied from DevTools into it.
   Save it.
2. Run:
       python scripts/setup_auth_from_file.py
3. On success this writes headers_auth.json and deletes raw_headers.txt
   (it's plaintext session auth -- no reason to leave it lying around).

Both raw_headers.txt and headers_auth.json are gitignored.
"""

import sys
from pathlib import Path

import ytmusicapi

RAW_PATH = Path("raw_headers.txt")


def main() -> int:
    if not RAW_PATH.exists():
        print(f"{RAW_PATH} not found. Paste your raw browser headers into that file first.")
        return 1

    raw = RAW_PATH.read_text()
    if not raw.strip():
        print(f"{RAW_PATH} is empty.")
        return 1

    try:
        ytmusicapi.setup(filepath="headers_auth.json", headers_raw=raw)
    except Exception as e:
        print(f"Failed to parse headers: {e}")
        return 1

    RAW_PATH.unlink()
    print("Saved auth to headers_auth.json and deleted raw_headers.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
