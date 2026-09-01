import os
import io
import re
import json
import logging
import asyncio
import time
import requests
from calendar import monthrange
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from PIL import Image
from dataclasses import dataclass

from urllib.parse import urlparse

from upload import export_receipt_row, remove_receipt_row, clear_receipt_export, export_uber_summary
from upload import google_sheets
from upload.google_drive_auth import (
    drive_upload_configured,
    oauth_partially_configured,
    using_delegated_for_drive,
    using_oauth_for_drive,
)

# --- ENVIRONMENT & CONFIGURATION ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


def normalize_turso_url(raw: str) -> str:
    """Turn Turso libsql/https URLs into the base HTTPS origin for the HTTP API."""
    url = raw.strip().strip("'\"")
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://") :]
    elif url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/").split("/v2/")[0]


# Ensure URL starts with https:// for Turso HTTP API
TURSO_URL = normalize_turso_url(os.getenv("TURSO_DATABASE_URL", ""))
TURSO_TOKEN = (os.getenv("TURSO_AUTH_TOKEN") or "").strip().strip("'\"")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()
model = genai.GenerativeModel(GEMINI_MODEL)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


def _gemini_error_message(exc: Exception) -> str | None:
    """User-facing message for common Gemini API failures, or None to use generic handling."""
    text = str(exc)
    lower = text.lower()
    if "404" in text and ("no longer available" in lower or "not found" in lower or "models/" in lower):
        return (
            "❌ **Gemini model not available** for your API key.\n\n"
            f"Current `GEMINI_MODEL`: `{GEMINI_MODEL}`.\n"
            "Set `GEMINI_MODEL` in `.env` to a model your project supports "
            "(e.g. `gemini-3.5-flash`, `gemini-1.5-flash`), then restart the bot."
        )
    if "429" in text or "quota" in lower or "rate" in lower:
        if re.search(r"limit:\s*0\b", text) or "perday" in lower.replace("_", ""):
            return (
                "⏳ **Gemini free-tier quota is used up** for "
                f"`{GEMINI_MODEL}` (daily/minute limit reached).\n\n"
                "Retries cannot fix this until quota resets or you upgrade. "
                "Enable billing in [Google AI Studio](https://aistudio.google.com/), "
                "wait for the daily reset, or set `GEMINI_MODEL` to another model with quota."
            )
        return (
            "⏳ **Gemini rate limit (429).** The bot will retry automatically when possible. "
            f"Model: `{GEMINI_MODEL}`. If this persists, check "
            "[rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)."
        )
    return None


def _gemini_retry_delay_seconds(exc: Exception) -> float | None:
    """Parse suggested retry delay from Gemini error text, if present."""
    text = str(exc)
    for pattern in (
        r"retry in (\d+(?:\.\d+)?)s",
        r"retry_delay[^\d]*(\d+(?:\.\d+)?)",
        r"seconds:\s*(\d+(?:\.\d+)?)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _gemini_error_is_retryable(exc: Exception) -> bool:
    """True for transient 503/429 rate limits; false for daily quota exhaustion."""
    text = str(exc)
    lower = text.lower()
    if "503" in text or "service unavailable" in lower or "overloaded" in lower:
        return True
    if "429" not in text and "resource exhausted" not in lower and "quota exceeded" not in lower:
        return False
    normalized = lower.replace("_", "").replace("-", "")
    if "requestsperday" in normalized or "perdayperproject" in normalized:
        return False
    if re.search(r"limit:\s*0\b", text):
        return False
    return True


def _log_gemini_retry_skip(exc: Exception) -> None:
    if "429" in str(exc) or "503" in str(exc):
        logging.info(
            "Gemini error not retrying (daily quota or hard limit): %s",
            exc,
        )


def generate_content_with_retry(contents, **kwargs):
    """Call Gemini with retries on transient 429/503 errors."""
    max_retries = max(1, int(os.getenv("GEMINI_MAX_RETRIES", "3")))
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return model.generate_content(contents, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not _gemini_error_is_retryable(exc):
                if attempt < max_retries:
                    _log_gemini_retry_skip(exc)
                raise
            delay = _gemini_retry_delay_seconds(exc)
            if delay is None:
                delay = float(min(2 ** (attempt - 1), 60))
            delay = min(max(delay, 1.0), 120.0)
            logging.warning(
                "Gemini API retryable error (attempt %s/%s), sleeping %.1fs: %s",
                attempt,
                max_retries,
                delay,
                exc,
            )
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Gemini generate_content failed without exception")


async def gemini_generate_content(contents, **kwargs):
    """Async wrapper so Telegram handlers do not block the event loop during retries."""
    return await asyncio.to_thread(generate_content_with_retry, contents, **kwargs)


@dataclass
class TursoResult:
    rows: list
    last_insert_rowid: int | None = None
    affected_row_count: int | None = None


# --- TURSO HTTP API HELPER ---
def _format_turso_arg(arg) -> dict:
    """Build a Turso/Hrana pipeline arg with the value type Turso expects.

    - float / REAL columns: JSON number (f64), not a string — pass Python float
    - integer columns: value as string (Hrana borrowed-string encoding)
    - text columns: Python str (merchant, date, item, category, etc.)
    """
    if arg is None:
        return {"type": "null"}
    # bool is a subclass of int — never send as integer/float.
    if isinstance(arg, bool):
        return {"type": "text", "value": str(arg)}
    if isinstance(arg, float):
        return {"type": "float", "value": float(arg)}
    if isinstance(arg, int):
        return {"type": "integer", "value": str(arg)}
    return {"type": "text", "value": str(arg)}


def execute_turso_sql(sql: str, args: list = None) -> TursoResult:
    """Executes SQL statements via Turso HTTP Pipeline API using indexed positional args and correct types."""
    if args is None:
        args = []

    formatted_args = [_format_turso_arg(arg) for arg in args]

    is_write = sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    # Wrap writes in an explicit transaction so the row is committed before we respond.
    if is_write:
        pipeline_requests = [
            {"type": "execute", "stmt": {"sql": "BEGIN", "args": []}},
            {
                "type": "execute",
                "stmt": {"sql": sql, "args": formatted_args},
            },
            {"type": "execute", "stmt": {"sql": "COMMIT", "args": []}},
            {"type": "close"},
        ]
        result_index = 1
    else:
        pipeline_requests = [
            {
                "type": "execute",
                "stmt": {"sql": sql, "args": formatted_args},
            },
            {"type": "close"},
        ]
        result_index = 0

    payload = {"requests": pipeline_requests}

    if not TURSO_URL or not TURSO_TOKEN:
        raise RuntimeError(
            "Turso is not configured. Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in .env "
            "(use the same values as on your OCI server)."
        )

    headers = {
        "Authorization": f"Bearer {TURSO_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post(f"{TURSO_URL}/v2/pipeline", json=payload, headers=headers)
    
    if not response.ok:
        if response.status_code == 404 and "Host not found" in response.text:
            host = urlparse(TURSO_URL).hostname or TURSO_URL
            raise Exception(
                f"Turso API Error (404): Host not found ({host}). "
                "TURSO_DATABASE_URL must be the database libsql URL from the Turso dashboard "
                "or `turso db show <db-name> --url` (e.g. libsql://my-db-youruser.turso.io), "
                "not your Google/GCP project name."
            )
        raise Exception(f"Turso API Error ({response.status_code}): {response.text}")

    data = response.json()
    for step in data.get("results", []):
        if step.get("type") == "error":
            raise Exception(step["error"]["message"])

    results = data["results"][result_index]
    stmt_result = results["response"]["result"]

    rows = []
    if "rows" in stmt_result:
        for row in stmt_result["rows"]:
            tuple_row = tuple(col.get("value") for col in row)
            rows.append(tuple_row)

    last_insert_rowid = stmt_result.get("last_insert_rowid")
    if last_insert_rowid is not None:
        last_insert_rowid = int(last_insert_rowid)

    return TursoResult(
        rows=rows,
        last_insert_rowid=last_insert_rowid,
        affected_row_count=stmt_result.get("affected_row_count"),
    )

# --- DATABASE SETUP ---
def _ensure_column(column_name: str, column_type: str) -> None:
    """Add a column to receipts if it does not already exist."""
    try:
        execute_turso_sql(
            f"ALTER TABLE receipts ADD COLUMN {column_name} {column_type}"
        )
        logging.info("Added receipts.%s column", column_name)
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return
        logging.warning("Could not ensure column %s: %s", column_name, exc)


def init_db():
    execute_turso_sql('''
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant TEXT,
            date TEXT,
            amount REAL,
            item TEXT,
            category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Existing databases created before item/category need these columns.
    _ensure_column("item", "TEXT")
    _ensure_column("category", "TEXT")

    execute_turso_sql('''
        CREATE TABLE IF NOT EXISTS earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            gross_earnings REAL NOT NULL,
            uber_fees REAL DEFAULT 0.0,
            net_payout REAL NOT NULL,
            tips REAL DEFAULT 0.0,
            source TEXT DEFAULT 'Uber Eats'
        )
    ''')
    logging.info("Ensured Turso tables: receipts, earnings")

def is_duplicate_receipt(merchant: str, date_str: str, amount: float) -> bool:
    """Checks cloud database for matching date & amount with flexible merchant matching."""
    core_merchant = merchant.split('(')[0].strip().lower()
    
    # Fetch receipts on the exact same date with the exact same amount
    sql = "SELECT merchant FROM receipts WHERE date = ?1 AND amount = ?2"
    result = execute_turso_sql(sql, [str(date_str), float(amount)])
    
    if not result.rows:
        return False
        
    for (existing_merchant,) in result.rows:
        existing_clean = existing_merchant.split('(')[0].strip().lower()
        if core_merchant in existing_clean or existing_clean in core_merchant:
            return True
            
    return False

def save_earnings_to_db(
    date_str: str,
    gross_earnings: float,
    uber_fees: float,
    net_payout: float,
    tips: float = 0.0,
    source: str = "Uber Eats",
) -> int | None:
    """Insert an Uber earnings row and return its id after commit + read-back."""
    sql = (
        "INSERT INTO earnings (date, gross_earnings, uber_fees, net_payout, tips, source) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6)"
    )
    result = execute_turso_sql(
        sql,
        [
            str(date_str),
            float(gross_earnings),
            float(uber_fees),
            float(net_payout),
            float(tips),
            str(source),
        ],
    )
    earnings_id = result.last_insert_rowid
    if earnings_id is None:
        logging.error(
            "Turso earnings INSERT returned no id for date=%s gross=%s",
            date_str,
            gross_earnings,
        )
        return None

    verify = execute_turso_sql(
        "SELECT id FROM earnings WHERE id = ?1;",
        [earnings_id],
    )
    if not verify.rows:
        logging.error("Turso earnings INSERT for id=%s was not visible on read-back", earnings_id)
        return None
    return earnings_id


def save_receipt_to_db(
    merchant: str,
    date_str: str,
    amount: float,
    item: str | None = None,
    category: str | None = None,
) -> int | None:
    """Insert a receipt and return its id only after the write has completed."""
    sql = (
        "INSERT INTO receipts (merchant, date, amount, item, category) "
        "VALUES (?1, ?2, ?3, ?4, ?5)"
    )
    # Text columns must always be strings (Gemini may return bare ints for dates/days).
    text_or_none = lambda v: None if v is None else str(v)
    result = execute_turso_sql(
        sql,
        [
            str(merchant),
            str(date_str),
            float(amount),
            text_or_none(item),
            text_or_none(category),
        ],
    )
    receipt_id = result.last_insert_rowid
    if receipt_id is None:
        logging.error(
            "Turso INSERT returned no last_insert_rowid for merchant=%s date=%s amount=%s",
            merchant,
            date_str,
            amount,
        )
        return None

    # Confirm the row is readable before callers reply to the user.
    verify = execute_turso_sql(
        "SELECT id FROM receipts WHERE id = ?1;",
        [receipt_id],
    )
    if not verify.rows:
        logging.error("Turso INSERT for id=%s was not visible on read-back", receipt_id)
        return None
    return receipt_id

def query_db(sql_query: str):
    try:
        return execute_turso_sql(sql_query).rows
    except Exception as e:
        return f"Database query error: {e}"

# --- HEALTH CHECK SERVER FOR RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/health", "/"]:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "bot": "running"}')
        else:
            self.send_response(404)
            self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- TELEGRAM COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi Justin! Upload a receipt photo to save it, a PDF weekly summary to calculate tax, or ask questions like 'How much did I spend on fuel this month?'\n\n"
        "Commands:\n"
        "• /undo - Remove the last uploaded receipt\n"
        "• /cleardb - Delete all receipts and start fresh\n"
        "• /dump - Show recent raw Turso receipt rows (for debugging)"
    )

async def undo_last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the single most recently added receipt entry."""
    status_message = await update.message.reply_text("⏳ Removing last uploaded receipt...")
    try:
        latest = execute_turso_sql(
            "SELECT id, merchant, date, amount FROM receipts ORDER BY id DESC LIMIT 1;"
        ).rows
        
        if not latest:
            await status_message.edit_text("ℹ️ Your database is currently empty. Nothing to undo.")
            return

        receipt_id, merchant, date_str, amount = latest[0]
        execute_turso_sql("DELETE FROM receipts WHERE id = ?1;", [receipt_id])
        sheets_removed = remove_receipt_row(int(receipt_id))
        sheets_note = "\n📊 Also removed from Google Sheets." if sheets_removed else ""

        reply = (
            "🗑️ **Last Receipt Removed!**\n\n"
            f"**Merchant:** {merchant}\n"
            f"**Date:** {date_str}\n"
            f"**Amount:** ${float(amount):.2f}"
            f"{sheets_note}"
        )
        await status_message.edit_text(reply, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Undo error: {e}")
        await status_message.edit_text(f"❌ Failed to remove last receipt: {str(e)}")

async def clear_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes all receipt records from the cloud database."""
    status_message = await update.message.reply_text("🧹 Clearing database...")
    try:
        execute_turso_sql("DELETE FROM receipts;")
        sheets_cleared = clear_receipt_export()
        extra = " Google Sheets tab was cleared too." if sheets_cleared else ""
        await status_message.edit_text(
            f"🗑️ **Database cleared successfully!** All receipt records have been removed.{extra}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.error(f"Clear DB error: {e}")
        await status_message.edit_text(f"❌ Failed to clear database: {str(e)}")


async def dump_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dump recent raw Turso receipt rows for debugging date/category storage."""
    status_message = await update.message.reply_text("🧾 Fetching recent Turso rows...")
    try:
        limit = 30
        if context.args:
            try:
                limit = max(1, min(100, int(context.args[0])))
            except ValueError:
                pass

        rows = execute_turso_sql(
            "SELECT id, merchant, date, amount, item, category, created_at "
            "FROM receipts ORDER BY id DESC LIMIT ?1;",
            [limit],
        ).rows

        logging.info("Turso /dump (%s rows): %s", len(rows), rows)

        if not rows:
            await status_message.edit_text("ℹ️ Turso `receipts` table is empty.")
            return

        lines = [
            f"🧾 **Turso dump** (latest {len(rows)} row(s))",
            "`id | merchant | date | amount | item | category | created_at`",
            "",
        ]
        for row in rows:
            rid, merchant, date_str, amount, item, category, created_at = row
            try:
                amount_fmt = f"{float(amount):.2f}"
            except (TypeError, ValueError):
                amount_fmt = str(amount)
            lines.append(
                f"`{rid}` | {merchant} | `{date_str}` | ${amount_fmt} | "
                f"{item or '—'} | {category or '—'} | `{created_at or '—'}`"
            )

        text = "\n".join(lines)
        # Telegram message limit ~4096 chars; split if needed.
        if len(text) <= 3900:
            await status_message.edit_text(text, parse_mode="Markdown")
            return

        await status_message.edit_text(text[:3900] + "\n…_(truncated)_", parse_mode="Markdown")
        for start in range(3900, len(text), 3900):
            await update.message.reply_text(text[start : start + 3900], parse_mode="Markdown")
    except Exception as e:
        logging.error("Dump DB error: %s", e)
        await status_message.edit_text(f"❌ Failed to dump database: {str(e)}")


# --- TELEGRAM MESSAGE HANDLERS ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = await update.message.reply_text("🔍 Analyzing receipt...")

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))

        prompt = f"""
        Analyze this image.
        Step 1: Check if it is a purchase receipt/invoice.
        Step 2: If NOT a receipt, return ONLY JSON: {{"is_receipt": false}}
        Step 3: If it IS a receipt, extract:
          - merchant
          - date as YYYY-MM-DD (or '{today_str}' if missing/unclear)
          - total paid amount (number only, e.g. 45.50)
          - item: brief item/description if clear (e.g. "Diesel", "Unleaded 91"), else null
          - category: one of fuel, food, parking, tolls, maintenance, other (lowercase), else null
        Return output STRICTLY as valid JSON with no markdown tags:
        {{"is_receipt": true, "merchant": "Store Name", "date": "YYYY-MM-DD", "amount": 45.50, "item": "Diesel", "category": "fuel"}}
        """

        response = await gemini_generate_content([prompt, image])
        clean_text = re.sub(r'```json|```', '', response.text).strip()
        data = json.loads(clean_text)

        if not data.get("is_receipt"):
            await status_message.edit_text("⚠️ This photo does not appear to be a receipt. Please upload a clear receipt image.")
            return

        merchant = str(data.get("merchant", "Unknown"))
        date_str = str(data.get("date", today_str))
        amount = float(data.get("amount", 0.0))
        item = data.get("item") or None
        category = data.get("category") or None
        if item is not None:
            item = str(item).strip() or None
        if category is not None:
            category = str(category).strip().lower() or None

        if is_duplicate_receipt(merchant, date_str, amount):
            header = (
                "⚠️ **Duplicate Receipt Detected!** "
                "(Match in Turso — not saved again; Google Sheets not updated.)\n\n"
            )
        else:
            receipt_id = save_receipt_to_db(
                merchant, date_str, amount, item=item, category=category
            )
            sheets_ok = False
            if receipt_id is not None:
                sheets_ok = export_receipt_row(
                    receipt_id,
                    merchant,
                    date_str,
                    amount,
                    image_bytes=bytes(photo_bytes),
                )
            if sheets_ok:
                header = "✅ **Receipt Saved** (Turso + Google Sheets)\n\n"
            elif receipt_id is not None:
                header = (
                    "✅ **Receipt Saved to Cloud Database!** "
                    "_(Google Sheets skipped duplicate or sync failed — check logs.)_\n\n"
                )
            else:
                header = "✅ **Receipt Saved to Cloud Database!**\n\n"

        reply = (
            f"{header}"
            f"**Merchant:** {merchant}\n"
            f"**Date:** {date_str}\n"
            f"**Total Paid:** ${amount:.2f}"
        )
        if item:
            reply += f"\n**Item:** {item}"
        if category:
            reply += f"\n**Category:** {category}"
        await status_message.edit_text(reply, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Receipt error: {e}")
        friendly = _gemini_error_message(e)
        await status_message.edit_text(friendly or f"❌ Error: {str(e)}", parse_mode="Markdown")

def _month_filter_examples(today: datetime) -> str:
    """Concrete month-range SQL examples for the SQL-generation prompt."""
    year, month = today.year, today.month
    last_day = monthrange(year, month)[1]
    month_prefix = f"{year}-{month:02d}"
    month_start = f"{month_prefix}-01"
    month_end = f"{month_prefix}-{last_day:02d}"
    mm = f"{month:02d}"
    return (
        f"Today is {today.strftime('%Y-%m-%d')}. "
        f"Dates may be stored as YYYY-MM-DD (e.g. '{month_start}') OR DD/MM/YYYY "
        f"(e.g. '01/{mm}/{year}'). "
        "For month filters, match BOTH formats with OR, for example for this month:\n"
        f"  (date >= '{month_start}' AND date <= '{month_end}')\n"
        f"  OR date LIKE '{month_prefix}%'\n"
        f"  OR date LIKE '__/{mm}/{year}'\n"
        f"  OR date LIKE '%/{mm}/{year}'\n"
        "For a named month like June (default year from context if unspecified):\n"
        f"  (date >= '{year}-06-01' AND date <= '{year}-06-30')\n"
        f"  OR date LIKE '{year}-06%'\n"
        f"  OR date LIKE '__/06/{year}'\n"
        f"  OR date LIKE '%/06/{year}'\n"
        "Never use strftime, MONTH(), or English month names in LIKE against the date column."
    )


def _fuel_filter_sql_guidance() -> str:
    """SQL patterns for fuel-related natural-language questions."""
    return (
        "For fuel / petrol / diesel / gas station questions, use case-insensitive "
        "wildcard matching across merchant, item, AND category, e.g.:\n"
        "  (\n"
        "    LOWER(COALESCE(merchant, '')) LIKE '%bp%'\n"
        "    OR LOWER(COALESCE(merchant, '')) LIKE '%shell%'\n"
        "    OR LOWER(COALESCE(merchant, '')) LIKE '%caltex%'\n"
        "    OR LOWER(COALESCE(merchant, '')) LIKE '%ampol%'\n"
        "    OR LOWER(COALESCE(merchant, '')) LIKE '%7-eleven%'\n"
        "    OR LOWER(COALESCE(merchant, '')) LIKE '%fuel%'\n"
        "    OR LOWER(COALESCE(merchant, '')) LIKE '%petrol%'\n"
        "    OR LOWER(COALESCE(category, '')) LIKE '%fuel%'\n"
        "    OR LOWER(COALESCE(item, '')) LIKE '%fuel%'\n"
        "    OR LOWER(COALESCE(item, '')) LIKE '%diesel%'\n"
        "    OR LOWER(COALESCE(item, '')) LIKE '%petrol%'\n"
        "    OR LOWER(COALESCE(item, '')) LIKE '%unleaded%'\n"
        "  )\n"
        "Combine fuel filters with month filters using AND when both are requested."
    )


async def handle_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    month_rules = _month_filter_examples(today)
    fuel_rules = _fuel_filter_sql_guidance()

    prompt = f"""
    You are an AI assistant with access to a cloud SQLite database table named 'receipts'.
    Table Schema:
    - id (INTEGER)
    - merchant (TEXT)
    - date (TEXT)  -- may be YYYY-MM-DD or DD/MM/YYYY
    - amount (REAL)
    - item (TEXT, nullable)  -- e.g. Diesel, Unleaded 91
    - category (TEXT, nullable)  -- e.g. fuel, food, parking

    Current Date Today: {today_str}

    Month filtering rules:
    {month_rules}

    Fuel / petrol search rules:
    {fuel_rules}

    User Question: "{user_query}"

    Task:
    1. Write a single SQLite SELECT statement to answer the question.
    2. When the question refers to a calendar month (e.g. "June", "this month", "last month"),
       filter dates flexibly for BOTH YYYY-MM-DD and DD/MM/YYYY as described above.
    3. When the question is about fuel/petrol/diesel/gas, use the fuel wildcard OR-group above
       so merchant, item, and category are all searched case-insensitively.
    4. Output ONLY the raw SQL query with no markdown, formatting, or extra text.
    """

    try:
        sql_response = await gemini_generate_content(prompt)
        raw_sql = sql_response.text.strip().replace("```sql", "").replace("```", "")

        query_results = query_db(raw_sql)

        summary_prompt = f"""
        User Question: "{user_query}"
        Executed SQL Query: {raw_sql}
        Database Output: {query_results}

        Formulate a polite, brief, and clear answer answering the user's question directly.
        """
        answer = await gemini_generate_content(summary_prompt)
        await update.message.reply_text(answer.text)

    except Exception as e:
        logging.error(f"Text query error: {e}")
        friendly = _gemini_error_message(e)
        await update.message.reply_text(friendly or f"❌ Database Error: {str(e)}", parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.file_name.endswith('.pdf'):
        await update.message.reply_text("⚠️ Please upload a valid PDF file.")
        return

    status_message = await update.message.reply_text("📄 Processing Uber summary...")

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        pdf_file = await document.get_file()
        pdf_bytes = await pdf_file.download_as_bytearray()

        pdf_part = {"mime_type": "application/pdf", "data": bytes(pdf_bytes)}
        prompt = f"""
        You are a tax assistant reading an Uber / Uber Eats weekly summary PDF.
        Extract payout figures and return STRICT JSON only (no markdown):
        {{
          "date": "YYYY-MM-DD",
          "gross_earnings": 0.00,
          "uber_fees": 0.00,
          "net_payout": 0.00,
          "tips": 0.00,
          "source": "Uber Eats"
        }}
        Rules:
        - date: week end date or statement date if available, else '{today_str}'
        - gross_earnings: total gross / Your Earnings (number only)
        - uber_fees: service/Uber fees if listed, else 0
        - net_payout: amount paid out / net if listed; else gross_earnings - uber_fees
        - tips: tips if listed, else 0
        - source: "Uber Eats" unless clearly Uber trips only, then "Uber"
        """

        response = await gemini_generate_content([prompt, pdf_part])
        clean_text = re.sub(r'```json|```', '', response.text).strip()
        data = json.loads(clean_text)

        date_str = str(data.get("date", today_str))
        gross_earnings = float(data.get("gross_earnings", 0.0) or 0.0)
        uber_fees = float(data.get("uber_fees", 0.0) or 0.0)
        net_payout = float(data.get("net_payout", 0.0) or 0.0)
        tips = float(data.get("tips", 0.0) or 0.0)
        source = str(data.get("source") or "Uber Eats")

        if net_payout <= 0 and gross_earnings > 0:
            net_payout = max(gross_earnings - uber_fees, 0.0)

        tax_set_aside = gross_earnings * 0.32

        earnings_id = save_earnings_to_db(
            date_str=date_str,
            gross_earnings=gross_earnings,
            uber_fees=uber_fees,
            net_payout=net_payout,
            tips=tips,
            source=source,
        )

        summary_text = (
            "📊 **Uber Weekly Summary**\n"
            f"**Date:** {date_str}\n"
            f"**Gross Earnings:** ${gross_earnings:.2f}\n"
            f"**Uber Fees:** ${uber_fees:.2f}\n"
            f"**Tips:** ${tips:.2f}\n"
            f"**Net Payout:** ${net_payout:.2f}\n"
            f"**Tax to Set Aside (32%):** ${tax_set_aside:.2f}\n"
            f"**Source:** {source}"
        )
        if earnings_id is not None:
            summary_text += f"\n\n💾 _Saved to Turso earnings (id={earnings_id})._"
        else:
            summary_text += "\n\n⚠️ _Could not save earnings row to Turso — check logs._"

        if export_uber_summary(summary_text):
            summary_text += "\n📊 _Copied to Google Sheets (Uber Summaries tab)._"

        await status_message.edit_text(summary_text, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"PDF error: {e}")
        friendly = _gemini_error_message(e)
        await status_message.edit_text(friendly or f"❌ Error: {str(e)}", parse_mode="Markdown")

# --- MAIN RUNNER ---
def _missing_required_env() -> list[str]:
    missing: list[str] = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not TURSO_URL:
        missing.append("TURSO_DATABASE_URL")
    if not TURSO_TOKEN:
        missing.append("TURSO_AUTH_TOKEN")
    return missing


def main():
    missing = _missing_required_env()
    if missing:
        logging.error("Cannot start bot — missing .env variables: %s", ", ".join(missing))
        raise SystemExit(1)

    init_db()
    Thread(target=run_http_server, daemon=True).start()

    logging.info("Gemini model: %s", GEMINI_MODEL)

    sheets_cfg = google_sheets.get_sheets_config()
    if sheets_cfg and sheets_cfg.enabled:
        logging.info(
            "Google Sheets export enabled (spreadsheet_id=%s…)",
            sheets_cfg.spreadsheet_id[:8],
        )
    else:
        logging.warning(
            "Google Sheets export is NOT configured — receipts will save to Turso only. "
            "Set GOOGLE_SHEETS_SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    if os.getenv("GOOGLE_DRIVE_RECEIPTS_FOLDER_ID", "").strip():
        if using_oauth_for_drive():
            logging.info("Google Drive receipt upload: OAuth user credentials")
        elif using_delegated_for_drive() or os.getenv("GOOGLE_DRIVE_FILE_OWNER_EMAIL", "").strip():
            logging.info("Google Drive receipt upload: service account with user delegation")
        elif drive_upload_configured():
            logging.warning(
                "Google Drive receipt upload: plain service account — personal My Drive folders "
                "need OAuth or GOOGLE_DRIVE_DELEGATED_USER_EMAIL (Workspace) + domain-wide delegation"
            )
        elif oauth_partially_configured():
            logging.warning(
                "Google Drive OAuth is incomplete on this host — set all of "
                "GOOGLE_DRIVE_OAUTH_CLIENT_ID, GOOGLE_DRIVE_OAUTH_CLIENT_SECRET, and "
                "GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN (or remove partial OAuth vars to use the service account)"
            )
        else:
            logging.warning(
                "GOOGLE_DRIVE_RECEIPTS_FOLDER_ID is set but Drive auth is missing — "
                "add GOOGLE_SERVICE_ACCOUNT_JSON (same as Sheets) or full GOOGLE_DRIVE_OAUTH_* trio"
            )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("undo", undo_last_command))
    app.add_handler(CommandHandler("cleardb", clear_db_command))
    app.add_handler(CommandHandler("dump", dump_db_command))

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_query))

    print("🤖 Bot is live with /undo, /cleardb, and /dump!")
    app.run_polling()

if __name__ == "__main__":
    main()
