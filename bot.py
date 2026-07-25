import os
import io
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from PIL import Image

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Send me a photo of a fuel or purchase receipt, and I'll analyze it for you."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_message = await update.message.reply_text("🔍 Analyzing receipt with Gemini...")

    try:
        # Get the highest resolution photo sent
        photo_file = await update.message.photo[-1].get_file()
        
        # Download photo directly into memory (BytesIO)
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))

        # Prompt for Gemini extraction
        prompt = (
            "Analyze this fuel/purchase receipt and extract the following details concisely:\n"
            "- Merchant / Store Name\n"
            "- Date and Time\n"
            "- Fuel Type & Volume (if applicable)\n"
            "- Total Amount Paid\n"
            "- Payment Method\n"
            "Format the output cleanly for a chat message."
        )

        # Call Gemini Vision Model
        response = model.generate_content([prompt, image])

        # Reply back to Telegram
        await status_message.edit_text(response.text)

    except Exception as e:
        logging.error(f"Error processing receipt: {e}")
        await status_message.edit_text(f"❌ Failed to process receipt: {str(e)}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
