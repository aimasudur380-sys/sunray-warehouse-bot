import os
import json
import base64
import logging
import re
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL")

def extract_do_data(base64_image):
    prompt = """Extract data from this Delivery Order image. Return ONLY valid JSON, no markdown, no explanation.
Format: {"documentNo":"","date":"dd/mm/yyyy","transactionType":"ISSUE","jobSite":"","projectName":"","items":[{"sku":"","description":"","qty":0,"uom":""}]}
Rules:
- SKU is the code after "SKU:" in each product Option line (e.g. "Option: 18MM - SKU: GTS-PK-18-00.00-" means sku = "GTS-PK-18-00.00-")
- transactionType: ISSUE for Delivery Order, INCOMING for Purchase Order or STK
- date from "Date Processed" field in dd/mm/yyyy format
- jobSite is the Project Code number
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
        timeout=60
    )

    data = response.json()
    logger.info(f"OpenRouter: {json.dumps(data)}")

    text = data["choices"][0]["message"]["content"]
    logger.info(f"AI output: {text}")

    clean = text.replace("```json", "").replace("```", "").strip()
    match = re.search(r'\{[\s\S]*\}', clean)
    if match:
        clean = match.group(0)

    return json.loads(clean)

def send_entry_to_sheet(entry_data):
    params = {
        "action": "entry",
        "date": entry_data.get("date", ""),
        "type": entry_data.get("transactionType", "ISSUE"),
        "docNo": entry_data.get("documentNo", ""),
        "jobSite": entry_data.get("jobSite", ""),
        "sku": entry_data.get("sku", ""),
        "qty": str(entry_data.get("qty", 0)),
        "uom": entry_data.get("uom", ""),
        "remark": entry_data.get("remark", "")
    }

    response = requests.get(APPS_SCRIPT_URL, params=params, timeout=30)
    return response.json()

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
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ কোনো item পাওয়া যায়নি।\nDebug: {json.dumps(extracted, ensure_ascii=False)}"
            )
            return

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

            result = send_entry_to_sheet(entry)
            logger.info(f"Sheet result: {result}")

            if result.get("success"):
                success_count += 1
                result_messages.append(f"✅ {item['sku']} | Qty: {item.get('qty')} | Balance: {result.get('newBalance')}")
            else:
                fail_count += 1
                result_messages.append(f"❌ {item['sku']}: {result.get('message', 'Unknown error')}")

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
        logger.error(f"Error: {e}", exc_info=True)
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
