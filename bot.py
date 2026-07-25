import os
import io
import re
import json
import sqlite3
import logging
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from PIL import Image

# Initialize Environment & Gemini
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect("receipts.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant TEXT,
            date TEXT,
            amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def is_duplicate_receipt(merchant: str, date_str: str, amount: float) -> bool:
    """Checks if a receipt with the exact same merchant, date, and amount already exists."""
    conn = sqlite3.connect("receipts.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM receipts WHERE LOWER(merchant) = LOWER(?) AND date = ? AND amount = ?",
        (merchant, date_str, amount)
    )
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_receipt_to_db(merchant: str, date_str: str, amount: float):
    conn = sqlite3.connect("receipts.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO receipts (merchant, date, amount) VALUES (?, ?, ?)",
        (merchant, date_str, amount)
    )
    conn.commit()
    conn.close()

def query_db(sql_query: str):
    conn = sqlite3.connect("receipts.db")
    cursor = conn.cursor()
    try:
        cursor.execute(sql_query)
        results = cursor.fetchall()
        conn.close()
        return results
    except Exception as e:
        conn.close()
        return f"Database query error: {e}"

# --- HEALTH CHECK SERVER ---
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

# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Upload a receipt photo to save it, a PDF weekly summary to calculate tax, or ask questions like 'How much did I spend on fuel this week?'"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = await update.message.reply_text("🔍 Analyzing receipt...")

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))

        # Prompt Gemini for structured extraction
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

        # Data-level duplicate check (Merchant + Date + Amount)
        if is_duplicate_receipt(merchant, date_str, amount):
            header = "⚠️ **Duplicate Receipt Detected!** (Exact match found in database)\n\n"
        else:
            save_receipt_to_db(merchant, date_str, amount)
            header = "✅ **Receipt Saved to Database!**\n\n"

        reply = (
            f"{header}"
            f"**Merchant:** {merchant}\n"
            f"**Date:** {date_str}\n"
            f"**Total Paid:** ${amount:.2f}"
        )
        await status_message.edit_text(reply, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Receipt error: {e}")
        await status_message.edit_text("❌ Failed to process or parse the receipt.")

async def handle_text_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text
    today_str = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""
    You are an AI assistant with access to an SQLite database table named 'receipts'.
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
        await update.message.reply_text("Sorry, I couldn't process that question from the database.")

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
        await status_message.edit_text(response.text, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"PDF error: {e}")
        await status_message.edit_text("❌ Failed to process PDF.")

# --- MAIN RUNNER ---
def main():
    Thread(target=run_http_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_query))

    print("🤖 Bot is live with Gemini 3.5 & Data Duplicate Protection!")
    app.run_polling()

if __name__ == "__main__":
    main()
