import os
import json
import base64
import logging
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials
import gspread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
MASTER_SHEET_NAME = "MASTER -LIST"

def get_google_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def extract_do_data(base64_image):
    prompt = """Extract data from this Delivery Order image. Return ONLY valid JSON, no markdown.
Format: {"documentNo":"","date":"dd/mm/yyyy","transactionType":"ISSUE","jobSite":"","projectName":"","items":[{"sku":"","description":"","qty":0,"uom":""}]}
Rules:
- SKU is the code after "SKU:" in each product Option line
- transactionType is ISSUE for Delivery Order, INCOMING for Purchase Order
- date from "Date Processed" field in dd/mm/yyyy format
- qty is number only"""

    payload = {
        "model": "meta-llama/llama-4-maverick:free",
        "max_tokens": 1500,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    }

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "HTTP-Referer": "https://sunray-warehouse-bot.onrender.com",
            "X-Title": "Sunray Warehouse Bot"
        },
        json=payload,
        timeout=30
    )

    data = response.json()
    logger.info(f"OpenRouter response: {data}")

    text = data["choices"][0]["message"]["content"]
    logger.info(f"AI output: {text}")

    clean = text.replace("```json", "").replace("```", "").strip()
    import re
    match = re.search(r'\{[\s\S]*\}', clean)
    if match:
        clean = match.group(0)

    return json.loads(clean)

def process_entry(client, entry_data):
    sh = client.open_by_key(SPREADSHEET_ID)
    master = sh.worksheet(MASTER_SHEET_NAME)
    master_data = master.get_all_values()

    sheet_name = None
    item_uom = None

    for row in master_data[1:]:
        if len(row) < 3:
            continue
        row_sku = row[2].strip().upper()
        if row_sku == entry_data["sku"].strip().upper():
            sheet_name = row[1] if len(row) > 1 else None
            item_uom = row[6] if len(row) > 6 else ""
            break

    if not sheet_name:
        return {"success": False, "message": f"SKU পাওয়া যায়নি: {entry_data['sku']}"}

    try:
        item_sheet = sh.worksheet(sheet_name.strip())
    except:
        return {"success": False, "message": f"Sheet নেই: {sheet_name}"}

    all_data = item_sheet.get_all_values()
    last_row = 4
    for r in range(len(all_data) - 1, 3, -1):
        row = all_data[r]
        has_data = (
            (len(row) > 0 and row[0] != "") or
            (len(row) > 2 and row[2] != "") or
            (len(row) > 6 and row[6] != "" and str(row[6]).replace('.','').lstrip('-').isdigit())
        )
        if has_data:
            last_row = r + 1
            break

    prev_balance = 0
    for r in range(last_row - 1, 3, -1):
        try:
            bal = all_data[r][6] if len(all_data[r]) > 6 else ""
            if bal != "" and str(bal).replace('.','').lstrip('-').isdigit():
                prev_balance = float(bal)
                break
        except:
            continue

    incoming_qty = float(entry_data["qty"]) if entry_data["transactionType"] == "INCOMING" else 0
    issue_qty = float(entry_data["qty"]) if entry_data["transactionType"] != "INCOMING" else 0
    new_balance = prev_balance + incoming_qty - issue_qty

    next_row = last_row + 1
    uom = entry_data.get("uom") or item_uom or ""

    row_data = [
        entry_data.get("date", ""),
        entry_data.get("jobSite", ""),
        entry_data.get("documentNo", ""),
        int(incoming_qty) if incoming_qty > 0 else "",
        int(issue_qty) if issue_qty > 0 else "",
        uom,
        int(new_balance),
        "",
        entry_data.get("remark", "")
    ]

    item_sheet.insert_row(row_data, next_row)

    return {"success": True, "newBalance": int(new_balance), "sheetName": sheet_name}

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text="⏳ ছবি processing হচ্ছে...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()
        base64_image = base64.b64encode(file_bytes).decode("utf-8")

        extracted = extract_do_data(base64_image)
        logger.info(f"Extracted: {extracted}")

        if not extracted or not extracted.get("items"):
            await context.bot.send_message(chat_id=chat_id, text=f"❌ কোনো item পাওয়া যায়নি।\n{json.dumps(extracted, ensure_ascii=False)}")
            return

        client = get_google_client()
        success_count = 0
        fail_count = 0
        result_messages = []

        for item in extracted["items"]:
            if not item.get("sku"):
                fail_count += 1
                continue

            entry = {
                "date": extracted.get("date", ""),
                "transactionType": extracted.get("transactionType", "ISSUE"),
                "documentNo": extracted.get("documentNo", ""),
                "jobSite": extracted.get("jobSite", ""),
                "sku": item["sku"],
                "qty": item.get("qty", 0),
                "uom": item.get("uom", ""),
                "remark": extracted.get("projectName", "")
            }

            result = process_entry(client, entry)
            if result["success"]:
                success_count += 1
                result_messages.append(f"✅ {item['sku']} | Qty: {item.get('qty')} | Balance: {result['newBalance']}")
            else:
                fail_count += 1
                result_messages.append(f"❌ {item['sku']}: {result['message']}")

        final_message = (
            f"📄 DO: {extracted.get('documentNo', 'N/A')}\n"
            f"📅 Date: {extracted.get('date', 'N/A')}\n"
            f"🏗️ Job Site: {extracted.get('jobSite', 'N/A')}\n"
            f"📋 Project: {extracted.get('projectName', 'N/A')}\n\n"
            + "\n".join(result_messages) +
            f"\n\n✅ সফল: {success_count} | ❌ ব্যর্থ: {fail_count}"
        )

        await context.bot.send_message(chat_id=chat_id, text=final_message)

    except Exception as e:
        logger.error(f"Error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📦 Sunray Warehouse Bot\n\nDO বা PO ছবি পাঠাও।\nBot automatically Sheet এ entry করে দেবে।"
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
