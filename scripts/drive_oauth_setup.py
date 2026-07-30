#!/usr/bin/env python3
"""One-time setup: authorize Google Drive and print a refresh token for .env.

Usage (local machine with browser):
  uv run python scripts/drive_oauth_setup.py

Add to .env on the server:
  GOOGLE_DRIVE_OAUTH_CLIENT_ID=...
  GOOGLE_DRIVE_OAUTH_CLIENT_SECRET=...
  GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN=...

Create OAuth client: Google Cloud Console → APIs & Services → Credentials →
OAuth 2.0 Client ID → Desktop app. Enable Google Drive API for the project.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    load_dotenv()
    client_id = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Set GOOGLE_DRIVE_OAUTH_CLIENT_ID and GOOGLE_DRIVE_OAUTH_CLIENT_SECRET in .env first "
            "(Desktop OAuth client from Google Cloud Console)."
        )

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0)

    print("\nAdd this to your server .env:\n")
    print(f"GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN={creds.refresh_token}\n")


if __name__ == "__main__":
    main()
