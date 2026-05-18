import os
import json
import re
import imaplib
import email
import asyncio
import logging
import hashlib
from io import BytesIO

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = "8890879470:AAHFTMhOdrJ80sLZOohu237t69ze4r9a55M"

DATA_FILE = "injector_data.json"
CHECK_INTERVAL_SECONDS = 10
IMAP_HOST = "imap.gmail.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

SEND_LOCK = asyncio.Lock()

PHONE_PATTERNS = [
    re.compile(r"Номер телефона:\s*(\+?7\d{10})", re.IGNORECASE),
    re.compile(r"Номер\s*телефона[\s:]+(\+?7\d{10})", re.IGNORECASE),
    re.compile(r"Телефон:\s*(\+?7\d{10})", re.IGNORECASE),
    re.compile(r"Номер:\s*(\+?7\d{10})", re.IGNORECASE),
    re.compile(r"(\+7\d{10})", re.IGNORECASE),
    re.compile(r"(79\d{9})", re.IGNORECASE),
    re.compile(r"(8\d{10})", re.IGNORECASE),
]


def load_data():
    if not os.path.exists(DATA_FILE):
        default = {
            "gmail_accounts": [],
            "last_check": {},
            "chat_id": None,
            "sent_messages": [],
            "deleted_messages": []
        }
        save_data(default)
        return default

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("gmail_accounts", [])
    data.setdefault("last_check", {})
    data.setdefault("chat_id", None)
    data.setdefault("sent_messages", [])
    data.setdefault("deleted_messages", [])
    return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_phone_from_text(text: str):
    if not text:
        return None
    for pattern in PHONE_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            digits = re.sub(r"\D", "", raw)
            if len(digits) == 11 and digits.startswith("8"):
                digits = "7" + digits[1:]
            elif len(digits) == 10 and digits.startswith("9"):
                digits = "7" + digits
            if re.fullmatch(r"79\d{9}", digits):
                return digits
    return None


def parse_message_content(msg):
    body_text = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition", "")).lower()

            if "attachment" in cdisp:
                continue

            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                if ctype == "text/plain":
                    body_text = payload.decode("utf-8", errors="ignore")
                elif ctype == "text/html":
                    body_html = payload.decode("utf-8", errors="ignore")
            except Exception as e:
                logger.error(f"Ошибка обработки части письма: {e}")
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                body_text = payload.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"Ошибка чтения тела письма: {e}")

    return body_text, body_html


def _collect_image_parts(msg):
    imgs = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
            continue
        try:
            data = part.get_payload(decode=True)
            if not data or len(data) < 200:
                continue
            filename = (part.get_filename() or "").strip().lower()
            cid = (part.get("Content-ID") or "").strip().strip("<>").strip().lower()
            cdisp = str(part.get("Content-Disposition", "")).lower()
            imgs.append({
                "data": data,
                "size": len(data),
                "filename": filename,
                "cid": cid,
                "disp": cdisp,
                "ctype": ctype
            })
        except Exception as e:
            logger.error(f"Ошибка чтения image part: {e}")
    return imgs


def find_qr_image_sync(msg):
    """
    Возвращает:
    - qr_io: BytesIO | None
    - warning: str | None
    Логика:
    1) Точно q935.png (из твоего eml)
    2) Иначе лучший кандидат, исключая qr-code.png (инструкция) и бордеры
    """
    images = _collect_image_parts(msg)
    if not images:
        return None, "⚠️ Картинка QR не найдена"

    # 1) Точное попадание
    exact = [i for i in images if i["filename"] == "q935.png" or i["cid"] == "q935.png"]
    if exact:
        exact.sort(key=lambda x: x["size"], reverse=True)
        return BytesIO(exact[0]["data"]), None

    # 2) Исключаем явный мусор
    filtered = []
    for i in images:
        tag = f'{i["filename"]} {i["cid"]}'
        if i["filename"] == "qr-code.png" or i["cid"] == "qr-code.png":
            continue
        if "border" in tag or "logo" in tag or "attention" in tag or "how_activate" in tag:
            continue
        if "instruction" in tag or "instr" in tag or "инструк" in tag:
            continue
        if "inline" not in i["disp"] and "attachment" not in i["disp"]:
            continue
        filtered.append(i)

    if filtered:
        filtered.sort(key=lambda x: x["size"], reverse=True)
        return BytesIO(filtered[0]["data"]), "⚠️ Отправлен fallback QR (не q935.png)"

    # 3) Последний fallback — крупнейшая картинка
    images.sort(key=lambda x: x["size"], reverse=True)
    return BytesIO(images[0]["data"]), "⚠️ Отправлена крупнейшая картинка (проверь вручную)"


def move_to_trash_sync(mail, num):
    try:
        result = mail.copy(num, "[Gmail]/Trash")
        if result[0] == "OK":
            mail.store(num, "+FLAGS", "\\Deleted")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка перемещения в корзину: {e}")
        return False


async def process_single_email(application: Application, chat_id: int, phone: str | None, qr_image_data: BytesIO | None, warn: str | None):
    if not qr_image_data:
        return False

    base_caption = phone if (phone and re.fullmatch(r"79\d{9}", phone)) else "Без номера"
    caption = f"{base_caption}\n{warn}" if warn else base_caption

    qr_image_data.seek(0)
    await application.bot.send_photo(
        chat_id=chat_id,
        photo=qr_image_data,
        caption=caption
    )
    return True


def fetch_events_sync():
    data = load_data()
    accounts = data.get("gmail_accounts", [])
    chat_id = data.get("chat_id")

    if not accounts or not chat_id:
        return {"events": [], "updated_data": data}

    sent_set = set(data.get("sent_messages", []))
    deleted_set = set(data.get("deleted_messages", []))
    events = []

    for account in accounts:
        email_addr = account.get("email")
        password = account.get("password")
        if not email_addr or not password:
            continue

        mail = None
        try:
            logger.info(f"Проверяем: {email_addr}")
            mail = imaplib.IMAP4_SSL(IMAP_HOST)
            mail.login(email_addr, password)
            mail.select("inbox")

            result, messages = mail.search(None, '(OR FROM "vm@mts.ru" FROM "vvm@mts.ru")')
            if result != "OK" or not messages or not messages[0]:
                continue

            for num in messages[0].split():
                result_msg, msg_data_full = mail.fetch(num, "(RFC822 UID)")
                if result_msg != "OK" or not msg_data_full or not msg_data_full[0]:
                    continue

                raw = msg_data_full[0][1]
                msg = email.message_from_bytes(raw)
                body_text, body_html = parse_message_content(msg)

                uid_match = re.search(r"UID (\d+)", str(msg_data_full[0][0]))
                uid = int(uid_match.group(1)) if uid_match else 0

                msg_id = (msg.get("Message-ID") or "").strip()
                if msg_id:
                    stable_id = msg_id
                else:
                    stable_id = hashlib.sha256(((body_text or "") + "\n" + (body_html or "")).encode("utf-8", errors="ignore")).hexdigest()

                message_key = f"{email_addr}_{stable_id}"
                if message_key in sent_set or message_key in deleted_set:
                    continue

                phone = extract_phone_from_text(body_html) or extract_phone_from_text(body_text)
                qr_io, warn = find_qr_image_sync(msg)

                if not qr_io:
                    logger.warning(f"QR не найден: {email_addr}, UID={uid}")
                    continue

                events.append({
                    "email_addr": email_addr,
                    "num": num,
                    "uid": uid,
                    "message_key": message_key,
                    "phone": phone,
                    "qr_image_data": qr_io,
                    "warn": warn
                })

        except Exception as e:
            logger.error(f"Ошибка IMAP {email_addr}: {e}")
        finally:
            try:
                if mail:
                    mail.close()
            except Exception:
                pass
            try:
                if mail:
                    mail.logout()
            except Exception:
                pass

    return {"events": events, "updated_data": data}


async def check_emails(application: Application):
    result = await asyncio.to_thread(fetch_events_sync)
    events = result["events"]
    data = result["updated_data"]

    chat_id = data.get("chat_id")
    if not chat_id:
        return

    sent_set = set(data.get("sent_messages", []))
    deleted_set = set(data.get("deleted_messages", []))

    grouped = {}
    for e in events:
        grouped.setdefault(e["email_addr"], []).append(e)

    for email_addr, email_events in grouped.items():
        account = next((a for a in data.get("gmail_accounts", []) if a.get("email") == email_addr), None)
        if not account:
            continue
        password = account.get("password")
        if not password:
            continue

        mail = None
        try:
            mail = await asyncio.to_thread(imaplib.IMAP4_SSL, IMAP_HOST)
            await asyncio.to_thread(mail.login, email_addr, password)
            await asyncio.to_thread(mail.select, "inbox")

            for e in email_events:
                try:
                    ok = await process_single_email(
                        application=application,
                        chat_id=int(chat_id),
                        phone=e.get("phone"),
                        qr_image_data=e.get("qr_image_data"),
                        warn=e.get("warn")
                    )
                except Exception as send_err:
                    logger.error(f"Ошибка отправки {e['message_key']}: {send_err}")
                    ok = False

                if ok:
                    sent_set.add(e["message_key"])  # антиспам
                    moved = await asyncio.to_thread(move_to_trash_sync, mail, e["num"])
                    if moved:
                        deleted_set.add(e["message_key"])
                    if e["uid"] > data["last_check"].get(email_addr, 0):
                        data["last_check"][email_addr] = e["uid"]

            await asyncio.to_thread(mail.expunge)

        except Exception as e:
            logger.error(f"Ошибка пост-обработки {email_addr}: {e}")
        finally:
            try:
                if mail:
                    await asyncio.to_thread(mail.close)
            except Exception:
                pass
            try:
                if mail:
                    await asyncio.to_thread(mail.logout)
            except Exception:
                pass

    data["sent_messages"] = list(sent_set)[-10000:]
    data["deleted_messages"] = list(deleted_set)[-10000:]
    save_data(data)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = str(update.effective_chat.id)
    save_data(data)

    await update.message.reply_text(
        "Бот запущен.\n"
        "Отправка: фото + номер.\n"
        "При сомнительном QR добавляется предупреждение в подписи.\n\n"
        "Команды:\n"
        "/addmail email password\n"
        "/listmails\n"
        "/removemail email\n"
        "/check\n"
        "/stats"
    )


async def add_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Пример: /addmail email@gmail.com app_password")
        return

    email_addr = context.args[0].strip()
    password = context.args[1].strip()

    data = load_data()
    if any(acc["email"] == email_addr for acc in data["gmail_accounts"]):
        await update.message.reply_text("Эта почта уже добавлена")
        return

    data["gmail_accounts"].append({"email": email_addr, "password": password})
    data["last_check"][email_addr] = data["last_check"].get(email_addr, 0)
    save_data(data)
    await update.message.reply_text(f"Добавлено: {email_addr}")


async def list_mails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    accounts = data.get("gmail_accounts", [])
    if not accounts:
        await update.message.reply_text("Почт нет")
        return

    sent_messages = data.get("sent_messages", [])
    text = "Почты:\n\n"
    for acc in accounts:
        email_addr = acc["email"]
        sent_count = sum(1 for m in sent_messages if m.startswith(f"{email_addr}_"))
        last_uid = data["last_check"].get(email_addr, 0)
        text += f"• {email_addr}\n  UID: {last_uid}\n  Отправлено: {sent_count}\n\n"
    await update.message.reply_text(text)


async def remove_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Пример: /removemail email@gmail.com")
        return

    email_addr = context.args[0].strip()
    data = load_data()
    before = len(data["gmail_accounts"])
    data["gmail_accounts"] = [a for a in data["gmail_accounts"] if a["email"] != email_addr]
    if email_addr in data["last_check"]:
        del data["last_check"][email_addr]
    save_data(data)

    if len(data["gmail_accounts"]) < before:
        await update.message.reply_text(f"Удалено: {email_addr}")
    else:
        await update.message.reply_text("Не найдено")


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю...")
    if SEND_LOCK.locked():
        await update.message.reply_text("Уже идёт проверка, подожди пару секунд.")
        return

    async with SEND_LOCK:
        await check_emails(context.application)

    await update.message.reply_text("Готово")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    await update.message.reply_text(
        f"Почт: {len(data.get('gmail_accounts', []))}\n"
        f"Отправлено: {len(data.get('sent_messages', []))}\n"
        f"Удалено: {len(data.get('deleted_messages', []))}"
    )


async def check_emails_job(context: ContextTypes.DEFAULT_TYPE):
    if SEND_LOCK.locked():
        return
    async with SEND_LOCK:
        try:
            await check_emails(context.application)
        except Exception as e:
            logger.error(f"Ошибка фоновой проверки: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addmail", add_mail))
    app.add_handler(CommandHandler("listmails", list_mails))
    app.add_handler(CommandHandler("removemail", remove_mail))
    app.add_handler(CommandHandler("check", check_now))
    app.add_handler(CommandHandler("stats", stats))

    app.job_queue.run_repeating(check_emails_job, interval=CHECK_INTERVAL_SECONDS, first=2)

    print("Бот запущен")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
