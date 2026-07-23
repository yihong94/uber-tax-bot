import os
import threading
from PIL import Image
from flask import Flask
import re
import pdfplumber
from google import genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Initialize Gemini Client (uses GEMINI_API_KEY environment variable)
ai_client = genai.Client()
# 1. Initialize Flask app
web_app = Flask(__name__)

@web_app.route('/')
@web_app.route('/health')
def health_check():
    # Returns a 200 OK status to cron-job.org
    return "Bot is alive!", 200

def run_web_server():
    # Render automatically sets the PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# paste your actual token inside these double quotes
TOKEN = os.environ.get("TELEGRAM_TOKEN")

TAX_RATE = 0.32


def extract_earnings_from_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text += text + "\n"

    patterns = [
        # Looks for "Your earnings" followed by an optional 'A', optional '$', and the amount
        r"Your\s*earnings\s*A?\$?\s*([\d,]+\.\d{2})",
        # Fallback for "Total earnings" with an optional 'A'
        r"Total\s*earnings\s*A?\$?\s*([\d,]+\.\d{2})",
        # Fallback for just "Total" or "Payout" with an optional 'A'
        r"(?:Total|Payout)\s*A?\$?\s*([\d,]+\.\d{2})"
    ]
   
    
    for pattern in patterns:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            raw_amount = match.group(1).replace(",", "")
            return float(raw_amount)
            
    return None

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads fuel receipt photos and extracts details using Gemini API."""
    photo = update.message.photo[-1]  # Get highest resolution
    photo_file = await photo.get_file()
    
    local_photo_path = f"temp_{photo.file_id}.jpg"
    
    try:
        await photo_file.download_to_drive(local_photo_path)
        await update.message.reply_text("Analyzing fuel receipt with Gemini...")

        # Open image with Pillow for Gemini SDK
        receipt_image = Image.open(local_photo_path)

        prompt = (
            "Analyze this fuel receipt image and extract the following details:\n"
            "- Total Amount Paid ($)\n"
            "- Date\n"
            "- Fuel Type (e.g., Diesel, Unleaded 91)\n"
            "- Litres Purchased\n\n"
            "Format the output as a clear summary."
        )

        # Call Gemini Vision model
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[receipt_image, prompt]
        )

        await update.message.reply_text(response.text)

    except Exception as e:
        await update.message.reply_text(f"Error reading receipt: {str(e)}")
        
    finally:
        if os.path.exists(local_photo_path):
            os.remove(local_photo_path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Send me your weekly Uber Eats PDF statement, "
        "and I'll calculate your tax!"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    
    if not document.file_name.lower().endswith('.pdf'):
        await update.message.reply_text("❌ Please send your weekly statement as a PDF file.")
        return
        
    await update.message.reply_text("📥 Statement received. Analyzing your earnings...")
    
    tg_file = await context.bot.get_file(document.file_id)
    local_pdf_path = f"temp_{document.file_name}"
    await tg_file.download_to_drive(local_pdf_path)
    
    try:
        earnings = extract_earnings_from_pdf(local_pdf_path)
        
        if earnings is not None:
            tax_to_save = earnings * TAX_RATE
            net_take_home = earnings - tax_to_save
            
            response_msg = (
                f"📊 *Weekly Tax Breakdown*\n\n"
                f"💰 *Total Earnings parsed:* ${earnings:,.2f}\n"
                f"🏦 *Tax Withholding (32%):* ${tax_to_save:,.2f}\n"
                f"💵 *Your Net Take-Home:* ${net_take_home:,.2f}\n\n"
                f"💡 _Tip: transfer ${tax_to_save:,.2f} to your tax savings account!_"
            )
            await update.message.reply_text(response_msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "⚠️ PDF read successfully, but I couldn't find a line saying 'Total earnings'. "
                "Make sure it's an official weekly summary statement!"
            )
            
    except Exception as e:
        await update.message.reply_text(f"❌ Error processing the PDF: {str(e)}")
        
    finally:
        if os.path.exists(local_pdf_path):
            os.remove(local_pdf_path)

def main():
    # Start web server in background thread for cron-job pinging
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    print("Health check web server started.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Add photo handler for receipts
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Bot is running successfully... Press Ctrl+C in this terminal to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()

