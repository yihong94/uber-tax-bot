import os
import io
import logging
from datetime import datetime
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from PIL import Image

# HTTP Server with Health Check Endpoint for cron-job.org
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" or self.path == "/":
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

# Load Environment Variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Send me a photo of a fuel receipt or upload your Uber weekly summary PDF."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = await update.message.reply_text("🔍 Analyzing image...")

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")

        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))

        # Smart prompt with non-receipt validation built-in
        prompt = (
            "Analyze this image carefully.\n"
            "Step 1: Check if this image is a purchase receipt, invoice, or docket.\n"
            "Step 2: If it is NOT a receipt/invoice, reply ONLY with: '⚠️ This photo does not appear to be a receipt. Please upload a clear image of a receipt.'\n"
            "Step 3: If it IS a receipt, extract the following 3 fields formatted EXACTLY like this:\n\n"
            "**Merchant:** [Store/Merchant Name]\n"
            "**Date:** [Date found on receipt in YYYY-MM-DD format, or " + today_str + " if not visible/found]\n"
            "**Total Paid:** [Total Amount with currency symbol]\n\n"
            "Do not include any extra text, intro, or explanation."
        )

        response = model.generate_content([prompt, image])
        await status_message.edit_text(response.text)

    except Exception as e:
        logging.error(f"Error processing receipt: {e}")
        await status_message.edit_text(f"❌ Failed to process image: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document.file_name.endswith('.pdf'):
        await update.message.reply_text("⚠️ Please upload a valid PDF file.")
        return

    status_message = await update.message.reply_text("📄 Processing Uber weekly summary PDF...")

    try:
        pdf_file = await document.get_file()
        pdf_bytes = await pdf_file.download_as_bytearray()

        pdf_part = {
            "mime_type": "application/pdf",
            "data": bytes(pdf_bytes)
        }

        prompt = (
            "You are a tax assistant reading an Uber Weekly Tax/Earnings Summary PDF.\n"
            "1. Locate the total gross earnings figure under 'Your Earnings' (or Total Gross Earnings).\n"
            "2. Calculate exactly 32% of that total earnings figure to set aside for tax.\n\n"
            "Return ONLY the response formatted like this:\n\n"
            "📊 **Uber Weekly Summary**\n"
            "**Total Earnings:** $[Amount]\n"
            "**Tax to Set Aside (32%):** $[Calculated 32% Amount]\n\n"
            "Do not add conversational fluff or intro text."
        )

        response = model.generate_content([prompt, pdf_part])
        await status_message.edit_text(response.text)

    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
        await status_message.edit_text(f"❌ Failed to process PDF: {str(e)}")

def main():
    Thread(target=run_http_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
