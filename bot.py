import os
import json
import io
import re
import threading
import tempfile
from datetime import datetime

# Third-party packages
import gspread
import pdfplumber
from PIL import Image
from flask import Flask
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# Telegram bot framework
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

# --- GOOGLE SERVICES HELPERS ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_google_credentials():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON environment variable not set.")
    creds_dict = json.loads(creds_json)
    return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

def is_duplicate_receipt(date_str, vendor_str, total_str):
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    sh = gc.open_by_key(sheet_id).sheet1
    
    records = sh.get_all_records()
    total_clean = total_str.replace("$", "").strip()
    
    for row in records:
        row_date = str(row.get("Date", "")).strip()
        row_vendor = str(row.get("Vendor", "")).strip().lower()
        row_total = str(row.get("Total", "")).replace("$", "").strip()
        
        if row_date == date_str and row_vendor == vendor_str.lower() and row_total == total_clean:
            return True
    return False

def append_to_sheet(date_str, vendor_str, total_str, drive_link=""):
    creds = get_google_credentials()
    gc = gspread.authorize(creds)
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    sh = gc.open_by_key(sheet_id).sheet1
    
    sh.append_row([date_str, vendor_str, f"${total_str.replace('$', '').strip()}", drive_link])

def upload_receipt_to_drive(file_bytes, file_name):
    creds = get_google_credentials()
    drive_service = build('drive', 'v3', credentials=creds)
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    
    file_metadata = {
        'name': file_name,
        'parents': [folder_id] if folder_id else []
    }
    
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype='image/jpeg')
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, webViewLink'
    ).execute()
    
    return uploaded_file.get('webViewLink')

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
    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    photo = update.message.photo[-1]  # Get highest resolution
    photo_file = await photo.get_file()

    try:
        # Download directly into memory buffer
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Prepare image for google.genai SDK safely in RAM
        receipt_image = types.Part.from_bytes(
            data=photo_bytes,
            mime_type="image/jpeg"
        )

        await update.message.reply_text("Analyzing fuel receipt with Gemini...")

        today_date = datetime.now().strftime("%d/%m/%y")
        prompt = (
            "Extract only the following details from this fuel receipt image:\n"
            "1. Vendor Name\n"
            "2. Total Amount Paid\n"
            f"3. Date (format as DD/MM/YY. If date is not visible or missing, use today's date: {today_date})\n\n"
            'Format your response strictly as JSON with key names: "vendor", "total", "date".\n'
            "Do not include markdown formatting or backticks around the JSON.\n"
            "Example format:\n"
            f'{{"vendor": "EG Cannonvale", "total": "48.30", "date": "{today_date}"}}'
        )

        # Call Gemini Vision model with retry logic
        client = genai.Client()
        max_retries = 3
        response = None

        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[receipt_image, prompt]
                )
                break  # Call succeeded, exit loop
            except Exception as e:
                if attempt < max_retries - 1:
                    import time
                    time.sleep(3)
                else:
                    raise e

        # Parse JSON output from Gemini (outside retry loop)
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw_text)

        vendor_val = str(data.get("vendor", "Unknown Vendor")).strip()
        total_val = str(data.get("total", "0.00")).replace("$", "").strip()
        date_val = str(data.get("date", today_date)).strip()

        # Check for duplicates in Google Sheets
        if is_duplicate_receipt(date_val, vendor_val, total_val):
            msg = (
                f"**Vendor:** {vendor_val}\n"
                f"**Total:** ${total_val}\n"
                f"**Date:** {date_val}\n\n"
                f"⚠️ *Duplicate detected - skipped Google Drive & Sheet update*"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        file_name = f"{date_val} fuel receipt.jpg".replace("/", "-")
        drive_link = upload_receipt_to_drive(photo_bytes, file_name)

        # Log receipt in Google Sheet
        append_to_sheet(date_val, vendor_val, total_val, drive_link)

        # Send clean response back to Telegram user
        clean_msg = (
            f"**Vendor:** {vendor_val}\n"
            f"**Total:** ${total_val}\n"
            f"**Date:** {date_val}"
        )
        await update.message.reply_text(clean_msg, parse_mode="Markdown")

    except Exception as e:
        err_msg = str(e) or repr(e) or type(e).__name__
        await update.message.reply_text(f"Error reading receipt: {err_msg}")


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
