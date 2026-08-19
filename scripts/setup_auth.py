"""
One-time interactive setup for ytmusicapi browser auth.

Run this yourself in a real terminal (it needs you to paste headers copied
from your browser's Network tab):

    python scripts/setup_auth.py

How to get the headers:
1. Open https://music.youtube.com in a browser and make sure you're logged in.
2. Open DevTools -> Network tab, filter by "browse".
3. Click any request to a "browse" endpoint (e.g. reload the page or click
   into a playlist to trigger one).
4. In the request's Headers panel, copy the *raw* request headers
   (Firefox: "Raw" toggle; Chrome: copy as cURL/raw from the headers list).
5. Paste them here when prompted, then press Enter followed by Ctrl-D
   (or an empty line, depending on your terminal).

This writes ./headers_auth.json, which is gitignored and must never be
committed or shared -- it's equivalent to your logged-in session.
"""

import ytmusicapi

if __name__ == "__main__":
    path = ytmusicapi.setup(filepath="headers_auth.json")
    print("\nSaved auth to headers_auth.json")
