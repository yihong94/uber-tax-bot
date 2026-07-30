"""Upload receipt images to Google Drive and return shareable view links."""

from __future__ import annotations

import io
import logging
import os
import re

from upload import google_sheets

logger = logging.getLogger(__name__)


def _drive_folder_id() -> str | None:
    folder_id = os.getenv("GOOGLE_DRIVE_RECEIPTS_FOLDER_ID", "").strip()
    return folder_id or None


def _safe_filename_part(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", value.strip())
    return cleaned[:max_len] or "receipt"


def upload_receipt_image(
    receipt_id: int,
    date_str: str,
    merchant: str,
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> str | None:
    """Upload receipt bytes to Drive. Returns a view URL or None if skipped/failed."""
    folder_id = _drive_folder_id()
    if not folder_id:
        logger.warning(
            "GOOGLE_DRIVE_RECEIPTS_FOLDER_ID is not set — skipping receipt image upload for id=%s",
            receipt_id,
        )
        return None

    if not google_sheets.get_sheets_config():
        logger.warning(
            "Google credentials not configured — skipping receipt image upload for id=%s",
            receipt_id,
        )
        return None

    ext = "jpg" if "jpeg" in mime_type else "png" if "png" in mime_type else "bin"
    name = (
        f"receipt_{receipt_id}_{date_str}_{_safe_filename_part(merchant)}.{ext}"
    )

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        credentials = google_sheets.get_service_account_credentials()
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)

        media = MediaIoBaseUpload(
            io.BytesIO(image_bytes),
            mimetype=mime_type,
            resumable=False,
        )
        created = (
            drive.files()
            .create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        file_id = created.get("id")
        if not file_id:
            logger.error("Drive upload returned no file id for receipt id=%s", receipt_id)
            return None

        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        ).execute()

        link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        logger.info("Uploaded receipt image to Drive for id=%s", receipt_id)
        return link
    except Exception as exc:
        logger.error("Google Drive receipt upload failed for id=%s: %s", receipt_id, exc)
        return None
