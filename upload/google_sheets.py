"""Google Sheets client for appending and managing exported receipt rows."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RECEIPTS_SHEET = "Receipts"
UBER_SHEET = "Uber Summaries"

RECEIPT_HEADERS = ["ID", "Merchant", "Date", "Amount", "Uploaded At"]
UBER_HEADERS = ["Uploaded At", "Summary Text"]


def _core_merchant(name: str) -> str:
    return name.split("(")[0].strip().lower()


def _merchants_match(a: str, b: str) -> bool:
    left, right = _core_merchant(a), _core_merchant(b)
    return left in right or right in left


def _amounts_match(stored: str, amount: float) -> bool:
    try:
        parsed = float(str(stored).replace("$", "").replace(",", "").strip())
    except ValueError:
        return False
    return abs(parsed - amount) < 0.01


def receipt_already_in_sheet(merchant: str, date_str: str, amount: float) -> bool:
    """True if the Receipts tab already has the same date, amount, and similar merchant."""
    worksheet = _worksheet(RECEIPTS_SHEET, RECEIPT_HEADERS)
    for row in worksheet.get_all_values()[1:]:
        if len(row) < 4:
            continue
        row_merchant, row_date, row_amount = row[1], row[2], row[3]
        if row_date.strip() != date_str.strip():
            continue
        if not _amounts_match(row_amount, amount):
            continue
        if _merchants_match(merchant, row_merchant):
            return True
    return False


@dataclass(frozen=True)
class SheetsConfig:
    spreadsheet_id: str
    enabled: bool


def _load_service_account_info() -> dict[str, Any] | None:
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        if (raw_json.startswith("'") and raw_json.endswith("'")) or (
            raw_json.startswith('"') and raw_json.endswith('"')
        ):
            raw_json = raw_json[1:-1]
        return json.loads(raw_json)

    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if credentials_path and os.path.isfile(credentials_path):
        with open(credentials_path, encoding="utf-8") as f:
            return json.load(f)

    return None


def get_sheets_config() -> SheetsConfig | None:
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        return None

    if not _load_service_account_info():
        return None

    enabled = os.getenv("GOOGLE_SHEETS_ENABLED", "true").lower() in ("1", "true", "yes")
    return SheetsConfig(spreadsheet_id=spreadsheet_id, enabled=enabled)


def _open_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials

    info = _load_service_account_info()
    if not info:
        raise RuntimeError("Google service account credentials are not configured.")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(credentials)
    config = get_sheets_config()
    if not config:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not set.")
    return client.open_by_key(config.spreadsheet_id)


def _worksheet(title: str, headers: list[str]):
    spreadsheet = _open_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(title)
    except Exception:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))

    existing = worksheet.row_values(1)
    if existing != headers:
        worksheet.update([headers], "A1")
    return worksheet


def append_receipt_row(
    receipt_id: int,
    merchant: str,
    date_str: str,
    amount: float,
    uploaded_at: str,
) -> bool:
    """Append a receipt row. Returns False if a matching row already exists."""
    if receipt_already_in_sheet(merchant, date_str, amount):
        logger.info(
            "Skipping duplicate Google Sheets receipt: merchant=%s date=%s amount=%s",
            merchant,
            date_str,
            amount,
        )
        return False

    worksheet = _worksheet(RECEIPTS_SHEET, RECEIPT_HEADERS)
    worksheet.append_row(
        [receipt_id, merchant, date_str, f"{amount:.2f}", uploaded_at],
        value_input_option="USER_ENTERED",
    )
    return True


def delete_receipt_row_by_id(receipt_id: int) -> bool:
    worksheet = _worksheet(RECEIPTS_SHEET, RECEIPT_HEADERS)
    ids = worksheet.col_values(1)
    for row_index, cell in enumerate(ids[1:], start=2):
        if str(cell).strip() == str(receipt_id):
            worksheet.delete_rows(row_index)
            return True
    return False


def clear_receipt_rows() -> None:
    worksheet = _worksheet(RECEIPTS_SHEET, RECEIPT_HEADERS)
    row_count = len(worksheet.get_all_values())
    if row_count > 1:
        worksheet.delete_rows(2, row_count)


def append_uber_summary(summary_text: str, uploaded_at: str) -> None:
    worksheet = _worksheet(UBER_SHEET, UBER_HEADERS)
    worksheet.append_row([uploaded_at, summary_text], value_input_option="USER_ENTERED")
