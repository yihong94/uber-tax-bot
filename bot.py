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
        "👋 Hi! Send me a photo of a receipt, and I'll analyze it for you."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = await update.message.reply_text("🔍 Analyzing receipt...")

    try:
        # Get current date as default
        today_str = datetime.now().strftime("%Y-%m-%d")

        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))

        # Strict & Clean Extraction Prompt
        prompt = (
            "Analyze this receipt image and return ONLY the following three fields formatted exactly like this:\n\n"
            "**Merchant:** [Store/Merchant Name]\n"
            "**Date:** [Date found on receipt in YYYY-MM-DD format, or " + today_str + " if not visible/found]\n"
            "**Total Paid:** [Total Amount with currency symbol]\n\n"
            "Do not include any extra text, intro, or explanation."
        )

        response = model.generate_content([prompt, image])
        await status_message.edit_text(response.text)

    except Exception as e:
        logging.error(f"Error processing receipt: {e}")
        await status_message.edit_text(f"❌ Failed to process receipt: {str(e)}")

def main():
    # Start health check server in background thread
    Thread(target=run_http_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
