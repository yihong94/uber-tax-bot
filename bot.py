import os
import io
import re
import json
import logging
import requests
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
model = genai.GenerativeModel('gemini-3.5-flash')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


@dataclass
class TursoResult:
    rows: list
    last_insert_rowid: int | None = None
    affected_row_count: int | None = None


# --- TURSO HTTP API HELPER ---
def execute_turso_sql(sql: str, args: list = None) -> TursoResult:
    """Executes SQL statements via Turso HTTP Pipeline API using indexed positional args and correct types."""
    if args is None:
        args = []

    formatted_args = []
    for arg in args:
        if isinstance(arg, float):
            formatted_args.append({"type": "float", "value": arg})
        elif isinstance(arg, int):
            formatted_args.append({"type": "integer", "value": arg})
        elif arg is None:
            formatted_args.append({"type": "null"})
        else:
            formatted_args.append({"type": "text", "value": str(arg)})

    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": formatted_args
                }
            },
            {"type": "close"}
        ]
    }

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
    results = data["results"][0]

    if results["type"] == "error":
        raise Exception(results["error"]["message"])

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
def init_db():
    execute_turso_sql('''
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant TEXT,
            date TEXT,
            amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

def is_duplicate_receipt(merchant: str, date_str: str, amount: float) -> bool:
    """Checks cloud database for matching date & amount with flexible merchant matching."""
    core_merchant = merchant.split('(')[0].strip().lower()
    
    # Fetch receipts on the exact same date with the exact same amount
    sql = "SELECT merchant FROM receipts WHERE date = ?1 AND amount = ?2"
    result = execute_turso_sql(sql, [date_str, amount])
    
    if not result.rows:
        return False
        
    for (existing_merchant,) in result.rows:
        existing_clean = existing_merchant.split('(')[0].strip().lower()
        if core_merchant in existing_clean or existing_clean in core_merchant:
            return True
            
    return False

def save_receipt_to_db(merchant: str, date_str: str, amount: float) -> int | None:
    sql = "INSERT INTO receipts (merchant, date, amount) VALUES (?1, ?2, ?3)"
    result = execute_turso_sql(sql, [merchant, date_str, amount])
    return result.last_insert_rowid

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
        "• /cleardb - Delete all receipts and start fresh"
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
        Step 3: If it IS a receipt, extract merchant, date (YYYY-MM-DD, or '{today_str}' if missing/unclear), and total paid amount (number only, e.g. 45.50).
        Return output STRICTLY as valid JSON with no markdown tags:
        {{"is_receipt": true, "merchant": "Store Name", "date": "YYYY-MM-DD", "amount": 45.50}}
        """

        response = model.generate_content([prompt, image])
        clean_text = re.sub(r'```json|```', '', response.text).strip()
        data = json.loads(clean_text)

        if not data.get("is_receipt"):
            await status_message.edit_text("⚠️ This photo does not appear to be a receipt. Please upload a clear receipt image.")
            return

        merchant = data.get("merchant", "Unknown")
        date_str = data.get("date", today_str)
        amount = float(data.get("amount", 0.0))

        if is_duplicate_receipt(merchant, date_str, amount):
            header = (
                "⚠️ **Duplicate Receipt Detected!** "
                "(Match in Turso — not saved again; Google Sheets not updated.)\n\n"
            )
        else:
            receipt_id = save_receipt_to_db(merchant, date_str, amount)
            sheets_ok = False
            if receipt_id is not None:
                sheets_ok = export_receipt_row(receipt_id, merchant, date_str, amount)
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
        await status_message.edit_text(reply, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Receipt error: {e}")
        await status_message.edit_text(f"❌ Error: {str(e)}")

async def handle_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text
    today_str = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""
    You are an AI assistant with access to a cloud SQLite database table named 'receipts'.
    Table Schema:
    - id (INTEGER)
    - merchant (TEXT)
    - date (TEXT, YYYY-MM-DD)
    - amount (REAL)

    Current Date Today: {today_str}

    User Question: "{user_query}"

    Task:
    1. Write a single SQLite SELECT statement to answer the question.
    2. Output ONLY the raw SQL query with no markdown, formatting, or extra text.
    """

    try:
        sql_response = model.generate_content(prompt)
        raw_sql = sql_response.text.strip().replace("```sql", "").replace("```", "")

        query_results = query_db(raw_sql)

        summary_prompt = f"""
        User Question: "{user_query}"
        Executed SQL Query: {raw_sql}
        Database Output: {query_results}

        Formulate a polite, brief, and clear answer answering the user's question directly.
        """
        answer = model.generate_content(summary_prompt)
        await update.message.reply_text(answer.text)

    except Exception as e:
        logging.error(f"Text query error: {e}")
        await update.message.reply_text(f"❌ Database Error: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.file_name.endswith('.pdf'):
        await update.message.reply_text("⚠️ Please upload a valid PDF file.")
        return

    status_message = await update.message.reply_text("📄 Processing Uber summary...")

    try:
        pdf_file = await document.get_file()
        pdf_bytes = await pdf_file.download_as_bytearray()

        pdf_part = {"mime_type": "application/pdf", "data": bytes(pdf_bytes)}
        prompt = (
            "You are a tax assistant reading an Uber Weekly Summary PDF.\n"
            "1. Find total gross earnings under 'Your Earnings'.\n"
            "2. Calculate 32% for tax set-aside.\n\n"
            "Return ONLY:\n"
            "📊 **Uber Weekly Summary**\n"
            "**Total Earnings:** $[Amount]\n"
            "**Tax to Set Aside (32%):** $[Calculated Amount]"
        )

        response = model.generate_content([prompt, pdf_part])
        summary_text = response.text
        if export_uber_summary(summary_text):
            summary_text = f"{summary_text}\n\n📊 _Copied to Google Sheets (Uber Summaries tab)._"
        await status_message.edit_text(summary_text, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"PDF error: {e}")
        await status_message.edit_text(f"❌ Error: {str(e)}")

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

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("undo", undo_last_command))
    app.add_handler(CommandHandler("cleardb", clear_db_command))

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_query))

    print("🤖 Bot is live with /undo and /cleardb functionality!")
    app.run_polling()

if __name__ == "__main__":
    main()
