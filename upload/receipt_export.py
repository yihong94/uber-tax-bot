"""Orchestrate exporting bot data to configured upload targets."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from upload import google_sheets

logger = logging.getLogger(__name__)


def _uploaded_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def export_receipt_row(
    receipt_id: int,
    merchant: str,
    date_str: str,
    amount: float,
) -> bool:
    """Push a saved receipt to Google Sheets when configured.

    Returns True if a row was appended, False if disabled, duplicate in sheet, or error.
    """
    config = google_sheets.get_sheets_config()
    if not config or not config.enabled:
        logger.warning(
            "Google Sheets export skipped for receipt id=%s (not configured or disabled)",
            receipt_id,
        )
        return False

    try:
        return google_sheets.append_receipt_row(
            receipt_id=receipt_id,
            merchant=merchant,
            date_str=date_str,
            amount=amount,
        )
    except Exception as exc:
        logger.error(
            "Google Sheets receipt export failed: %s — "
            "check GOOGLE_SHEETS_SPREADSHEET_ID and that the sheet is shared with the service account email.",
            exc,
        )
        return False


def remove_receipt_row(receipt_id: int) -> bool:
    config = google_sheets.get_sheets_config()
    if not config or not config.enabled:
        return False

    try:
        return google_sheets.delete_receipt_row_by_id(receipt_id)
    except Exception as exc:
        logger.error("Google Sheets receipt delete failed: %s", exc)
        return False


def clear_receipt_export() -> bool:
    config = google_sheets.get_sheets_config()
    if not config or not config.enabled:
        return False

    try:
        google_sheets.clear_receipt_rows()
        return True
    except Exception as exc:
        logger.error("Google Sheets clear failed: %s", exc)
        return False


def export_uber_summary(summary_text: str) -> bool:
    config = google_sheets.get_sheets_config()
    if not config or not config.enabled:
        return False

    try:
        google_sheets.append_uber_summary(summary_text=summary_text, uploaded_at=_uploaded_at())
        return True
    except Exception as exc:
        logger.error("Google Sheets Uber summary export failed: %s", exc)
        return False
