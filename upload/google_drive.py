"""Upload receipt images to Google Drive and return shareable view links."""

from __future__ import annotations

import io
import logging
import os
import re
from typing import Any

from upload.google_drive_auth import (
    drive_upload_configured,
    get_drive_credentials,
    preferred_delegation_subject,
    using_delegated_for_drive,
    using_oauth_for_drive,
    _file_owner_email,
)

logger = logging.getLogger(__name__)


def _drive_folder_id() -> str | None:
    folder_id = os.getenv("GOOGLE_DRIVE_RECEIPTS_FOLDER_ID", "").strip()
    return folder_id or None


def _safe_filename_part(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", value.strip())
    return cleaned[:max_len] or "receipt"


def _folder_metadata(drive: Any, folder_id: str) -> dict[str, Any]:
    return (
        drive.files()
        .get(
            fileId=folder_id,
            fields="id,name,driveId,owners(emailAddress),capabilities",
            supportsAllDrives=True,
        )
        .execute()
    )


def _user_owner_from_folder(folder: dict[str, Any]) -> str | None:
    for owner in folder.get("owners") or []:
        email = (owner.get("emailAddress") or "").strip()
        if email and not email.endswith(".gserviceaccount.com"):
            return email
    return None


def _create_receipt_file(
    drive: Any,
    *,
    name: str,
    folder_id: str,
    image_bytes: bytes,
    mime_type: str,
) -> dict[str, Any]:
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(
        io.BytesIO(image_bytes),
        mimetype=mime_type,
        resumable=False,
    )
    return (
        drive.files()
        .create(
            body={"name": name, "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink, owners(emailAddress)",
            supportsAllDrives=True,
        )
        .execute()
    )


def _transfer_file_ownership(drive: Any, file_id: str, owner_email: str) -> None:
    drive.permissions().create(
        fileId=file_id,
        transferOwnership=True,
        body={"type": "user", "role": "owner", "emailAddress": owner_email},
        supportsAllDrives=True,
    ).execute()
    logger.info("Transferred Drive file %s ownership to %s", file_id, owner_email)


def _ensure_parent_context_and_permissions(
    drive: Any,
    file_id: str,
    folder_id: str,
    folder: dict[str, Any],
    created: dict[str, Any],
) -> None:
    """Keep file under the receipts folder and assign human ownership when possible."""
    owner_target = preferred_delegation_subject(_user_owner_from_folder(folder))
    if not owner_target:
        return

    current_owners = created.get("owners") or []
    already_owned = any(
        (o.get("emailAddress") or "").lower() == owner_target.lower()
        for o in current_owners
    )
    if not already_owned:
        try:
            _transfer_file_ownership(drive, file_id, owner_target)
        except Exception as exc:
            logger.warning(
                "Could not transfer Drive file %s to %s (parent folder %s): %s",
                file_id,
                owner_target,
                folder_id,
                exc,
            )

    try:
        parents = (
            drive.files()
            .get(fileId=file_id, fields="parents", supportsAllDrives=True)
            .execute()
            .get("parents")
            or []
        )
        if folder_id not in parents:
            drive.files().update(
                fileId=file_id,
                addParents=folder_id,
                supportsAllDrives=True,
                fields="id, parents",
            ).execute()
    except Exception as exc:
        logger.warning("Could not confirm parent folder for Drive file %s: %s", file_id, exc)


def _upload_with_credentials(
    credentials: Any,
    *,
    name: str,
    folder_id: str,
    folder: dict[str, Any],
    image_bytes: bytes,
    mime_type: str,
) -> dict[str, Any] | None:
    from googleapiclient.discovery import build

    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    created = _create_receipt_file(
        drive,
        name=name,
        folder_id=folder_id,
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    file_id = created.get("id")
    if not file_id:
        return None

    _ensure_parent_context_and_permissions(drive, file_id, folder_id, folder, created)

    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()

    return created


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

    if not drive_upload_configured():
        logger.warning(
            "Google Drive upload not configured — skipping receipt image upload for id=%s",
            receipt_id,
        )
        return None

    ext = "jpg" if "jpeg" in mime_type else "png" if "png" in mime_type else "bin"
    name = f"receipt_{receipt_id}_{date_str}_{_safe_filename_part(merchant)}.{ext}"

    try:
        from googleapiclient.discovery import build

        probe_drive = build(
            "drive",
            "v3",
            credentials=get_drive_credentials(),
            cache_discovery=False,
        )
        folder = _folder_metadata(probe_drive, folder_id)
        folder_owner = _user_owner_from_folder(folder)
        if folder.get("driveId"):
            logger.debug("Receipt folder is on Shared Drive %s", folder["driveId"])

        delegation_subject = preferred_delegation_subject(folder_owner)
        if using_oauth_for_drive():
            credentials = get_drive_credentials()
            created = _upload_with_credentials(
                credentials,
                name=name,
                folder_id=folder_id,
                folder=folder,
                image_bytes=image_bytes,
                mime_type=mime_type,
            )
        elif delegation_subject:
            credentials = get_drive_credentials(delegation_subject=delegation_subject)
            created = _upload_with_credentials(
                credentials,
                name=name,
                folder_id=folder_id,
                folder=folder,
                image_bytes=image_bytes,
                mime_type=mime_type,
            )
        else:
            try:
                created = _upload_with_credentials(
                    get_drive_credentials(),
                    name=name,
                    folder_id=folder_id,
                    folder=folder,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                )
            except Exception as first_exc:
                if "storageQuotaExceeded" not in str(first_exc) or not folder_owner:
                    raise
                logger.info(
                    "Retrying Drive upload for receipt id=%s as folder owner %s",
                    receipt_id,
                    folder_owner,
                )
                created = _upload_with_credentials(
                    get_drive_credentials(delegation_subject=folder_owner),
                    name=name,
                    folder_id=folder_id,
                    folder=folder,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                )

        if not created:
            logger.error("Drive upload returned no file id for receipt id=%s", receipt_id)
            return None

        file_id = created["id"]
        link = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        auth_mode = (
            "oauth"
            if using_oauth_for_drive()
            else "delegated"
            if using_delegated_for_drive() or delegation_subject
            else "service_account"
        )
        logger.info("Uploaded receipt image to Drive for id=%s (auth=%s)", receipt_id, auth_mode)
        return link
    except Exception as exc:
        err_text = str(exc)
        if "storageQuotaExceeded" in err_text:
            owner_hint = _file_owner_email() or "the Google account that owns the folder"
            logger.error(
                "Google Drive upload failed for receipt id=%s: files cannot be created in a "
                "personal folder using a bare service account. Set GOOGLE_DRIVE_OAUTH_* "
                "(personal Gmail, see scripts/drive_oauth_setup.py), or for Workspace set "
                "GOOGLE_DRIVE_DELEGATED_USER_EMAIL / GOOGLE_DRIVE_FILE_OWNER_EMAIL to %s and "
                "enable domain-wide delegation on the service account. Error: %s",
                receipt_id,
                owner_hint,
                exc,
            )
        elif "notFound" in err_text or "404" in err_text:
            logger.error(
                "Google Drive receipt upload failed for id=%s: folder not found or not shared "
                "with the uploader (GOOGLE_DRIVE_RECEIPTS_FOLDER_ID=%s). Error: %s",
                receipt_id,
                folder_id,
                exc,
            )
        else:
            logger.error("Google Drive receipt upload failed for id=%s: %s", receipt_id, exc)
        return None
