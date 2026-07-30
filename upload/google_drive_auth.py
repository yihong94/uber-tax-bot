"""Resolve credentials for Google Drive uploads (OAuth user or service account)."""

from __future__ import annotations

import logging
import os

from google.oauth2 import service_account

from upload import google_sheets

logger = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def oauth_partially_configured() -> bool:
    """OAuth vars present but not the full set required for Drive upload."""
    present = sum(
        1
        for v in (_oauth_refresh_token(), _oauth_client_id(), _oauth_client_secret())
        if v
    )
    return 0 < present < 3


def drive_upload_configured() -> bool:
    """True if Drive upload can run (full OAuth trio or service account)."""
    if using_oauth_for_drive():
        return True
    if google_sheets.get_sheets_config() is not None:
        return True
    return False


def using_oauth_for_drive() -> bool:
    return bool(
        _oauth_refresh_token() and _oauth_client_id() and _oauth_client_secret()
    )


def using_delegated_for_drive() -> bool:
    return bool(_delegated_user_email())


def _oauth_refresh_token() -> str:
    return os.getenv("GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN", "").strip()


def _oauth_client_id() -> str:
    return os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_ID", "").strip()


def _oauth_client_secret() -> str:
    return os.getenv("GOOGLE_DRIVE_OAUTH_CLIENT_SECRET", "").strip()


def _delegated_user_email() -> str:
    return os.getenv("GOOGLE_DRIVE_DELEGATED_USER_EMAIL", "").strip()


def _file_owner_email() -> str:
    return os.getenv("GOOGLE_DRIVE_FILE_OWNER_EMAIL", "").strip()


def service_account_drive_credentials(*, subject: str | None = None):
    """Service account credentials with Drive scope; optional domain-wide delegation subject."""
    info = google_sheets.load_service_account_info()
    if not info:
        raise RuntimeError("Google service account credentials are not configured.")
    return service_account.Credentials.from_service_account_info(
        info,
        scopes=DRIVE_SCOPES,
        subject=subject,
    )


def get_drive_credentials(*, delegation_subject: str | None = None):
    """OAuth user, delegated service account, or plain service account."""
    refresh = _oauth_refresh_token()
    client_id = _oauth_client_id()
    client_secret = _oauth_client_secret()
    if refresh and client_id and client_secret:
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=refresh,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=DRIVE_SCOPES,
        )

    subject = delegation_subject or _delegated_user_email() or None
    if subject:
        logger.debug("Using domain-wide delegation for Drive as %s", subject)
        return service_account_drive_credentials(subject=subject)

    return service_account_drive_credentials()


def preferred_delegation_subject(folder_user_owner: str | None = None) -> str | None:
    """Email to impersonate when uploading into a user-owned folder."""
    for candidate in (_delegated_user_email(), _file_owner_email(), folder_user_owner):
        if candidate and not candidate.endswith(".gserviceaccount.com"):
            return candidate
    return None
