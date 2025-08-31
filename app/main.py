# app/main.py
import os
import io
import re
import json
import sys
import subprocess
import tempfile
import traceback
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update,
    BufferedInputFile,
)

# =========================
# KONFIG (env kerak emas)
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"  # trailing slashsiz
GROUP_CHAT_ID = -1003046464831  # ma'lumot jo'natiladigan guruh

# =========================
# AIROGRAM
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ACTIVE_USERS = set()


@dp.message(Command("start"))
async def start_cmd(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    text = (
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "/new_resume - Yangi obektivka\n"
        "/help - Yordam\n\n"
        "@O_P_admin"
    )
    await m.answer(text)


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("Savol bo‘lsa yozing: @O_P_admin")


@dp.message(Command("new_resume"))
async def new_resume_cmd(m: Message):
    base = (APP_BASE or "").rstrip("/")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Obyektivkani to‘ldirish",
                web_app=WebAppInfo(url=f"{base}/form?id={m.from_user.id}")
            )
        ]]
    )
    txt = ("👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
           "✅ Tez\n✅ Oson\n✅ Ishonchli\n"
           "quyidagi 🌐 web formani to'ldiring\n👇👇👇👇👇👇👇👇👇")
    await m.answer(txt, reply_markup=kb)


# =========================
# FASTAPI
# =========================
app = FastAPI()


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    # 500 o'rniga JSON qaytaramiz (web app alert uchun qulay)
    print("=== GLOBAL ERROR ===", file=sys.stderr)
    print(repr(exc), file=sys.stderr)
    traceback.print_exc()
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)


@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"


@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)


# =========================
# YORDAMCHI: Fayl nomi generatori
# =========================
def make_safe_basename(full_name: str, phone: str) -> str:
    base = "_".join((full_name or "user").strip().split())
    base = re.sub(r"[^A-Za-z0-9_]+", "", base) or "user"
    ph = (phone or "").strip() or "NaN"
    dm = datetime.utcnow().strftime("%d-%m")
    return f"{base}_{ph}_{dm}".lower()


def pick_image_ext(upload_name: str | None) -> str:
    ext = (os.path.splitext(upload_name or "")[1] or "").lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ext
    return ".png"


# =========================
# DOCX -> PDF (LibreOffice)
# =========================
def convert_docx_to_pdf(docx_bytes: bytes) -> Optional[bytes]:
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "resume.docx")
        pdf_path = os.path.join(tmpdir, "resume.pdf")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, docx_path],
                check=True
            )
            with open(pdf_path, "rb") as f:
                return f.read()
        except Exception as e:
            print("DOCX->PDF ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None


# =========================
# FORMA QABUL QILISH (DB yo‘q)
# =========================
@app.post("/send_resume_data")
async def send_resume_data(
    full_name: str = Form(...),
    phone: str = Form(...),
    tg_id: str = Form(...),
    birth_date: str = Form(""),
    birth_place: str = Form(""),
    nationality: str = Form("O‘zbek"),
    party_membership: str = Form("Yo‘q"),
    education: str = Form(""),
    university: str = Form(""),
    specialization: str = Form("Yo‘q"),
    ilmiy_daraja: str = Form("Yo‘q"),
    ilmiy_unvon: str = Form("Yo‘q"),
    languages: str = Form("Yo‘q"),
    dav_mukofoti: str = Form("Yo‘q"),
    deputat: str = Form("Yo‘q"),
    adresss: str = Form(""),
    current_position_date: str = Form(""),
    current_position_full: str = Form(""),
    work_experience: str = Form(""),
    relatives: str = Form("[]"),
    photo: UploadFile | None = None,
):
    # 1) relatives JSON
    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

    # 2) template tekshir
    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx template topilmadi"}, status_code=200)

    # 3) context
    ctx = {
        "full_name": full_name,
        "phone": phone,
        "birth_date": birth_date,
        "birth_place": birth_place,
        "nationality": nationality,
        "party_membership": party_membership,
        "education": education,
        "university": university,
        "specialization": specialization,
        "ilmiy_daraja": ilmiy_daraja,
        "ilmiy_unvon": ilmiy_unvon,
        "languages": languages,
        "dav_mukofoti": dav_mukofoti,
        "deputat": deputat,
        "adresss": adresss,
        "current_position_date": current_position_date,
        "current_position_full": current_position_full,
        "work_experience": work_experience,
        "relatives": rels,
    }

    # 4) DOCX render + rasm (ixtiyoriy)
    doc = DocxTemplate(tpl_path)
    inline_img = None
    img = None  # guruhga file sifatida yuborish uchun
    img_ext = ".png"
    try:
        if photo is not None and getattr(photo, "filename", ""):
            img = await photo.read()
            img_ext = pick_image_ext(photo.filename)
            if img:
                inline_img = InlineImage(doc, io.BytesIO(img), width=Mm(35))
    except Exception as e:
        print("PHOTO ERROR:", repr(e), file=sys.stderr)

    ctx["photo"] = inline_img

    # 5) DOCX bytes
    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()

    # 6) PDF bytes
    pdf_bytes = convert_docx_to_pdf(docx_bytes)

    # 7) nomlar
    base_name = make_safe_basename(full_name, phone)
    docx_name = f"{base_name}_0.docx"
    pdf_name = f"{base_name}_0.pdf"
    img_name = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # 8) GURUHGA: rasmni ham, jsonni ham FILE (document) sifatida yuboramiz
    try:
        # a) rasm (agar bor bo'lsa) — document ko'rinishida (filename saqlanadi)
        if img:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img, filename=img_name),
                caption=f"🆕 Yangi forma: {full_name}\n📞 {phone}\n👤 TG: {tg_id}"
            )

        # b) json — alohida document
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "tg_id": tg_id,
            "full_name": full_name,
            "phone": phone,
            "birth_date": birth_date,
            "birth_place": birth_place,
            "nationality": nationality,
            "party_membership": party_membership,
            "education": education,
            "university": university,
            "specialization": specialization,
            "ilmiy_daraja": ilmiy_daraja,
            "ilmiy_unvon": ilmiy_unvon,
            "languages": languages,
            "dav_mukofoti": dav_mukofoti,
            "deputat": deputat,
            "adresss": adresss,
            "current_position_date": current_position_date,
            "current_position_full": current_position_full,
            "work_experience": work_experience,
            "relatives": rels,
        }
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await bot.send_document(
            GROUP_CHAT_ID,
            BufferedInputFile(json_bytes, filename=json_name),
            caption=f"📄 Ma'lumotlar JSON: {full_name}"
        )
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()

    # 9) MIJOZGA: DOCX + PDF
    try:
        chat_id = int(tg_id)
        await bot.send_document(
            chat_id,
            BufferedInputFile(docx_bytes, filename=docx_name),
            caption="✅ Word formatdagi rezyume"
        )
        if pdf_bytes:
            await bot.send_document(
                chat_id,
                BufferedInputFile(pdf_bytes, filename=pdf_name),
                caption="✅ PDF formatdagi rezyume"
            )
        else:
            await bot.send_message(chat_id, "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi.")
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=200)

    return {"status": "success"}


# =========================
# WEBHOOK
# =========================
@app.post("/bot/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    try:
        if hasattr(dp, "feed_raw_update"):
            await dp.feed_raw_update(bot, data)
        else:
            update = Update.model_validate(data)
            await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print("=== WEBHOOK ERROR ===", file=sys.stderr)
        print(repr(e), file=sys.stderr)
        traceback.print_exc()
        print("Update JSON:", data, file=sys.stderr)
        return {"ok": False}


@app.get("/bot/set_webhook")
async def set_webhook(base: str | None = None):
    base_url = (base or APP_BASE).rstrip("/")
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}


# =========================
# DEBUG
# =========================
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}


@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}
