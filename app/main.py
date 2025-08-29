# app/main.py
import os
import io
import json
import sys
import subprocess
import tempfile
import traceback


from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update
from aiogram.types import BufferedInputFile

# =========================
# CONFIG
# =========================
# Bot token (o'zing bergan)
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
# Railway domeningni xohlasang APP_BASE env orqali berasan:
APP_BASE = os.getenv("APP_BASE", "https://ofmbot-production.up.railway.app")

# =========================
# BOT SETUP
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Oddiy "faol foydalanuvchi" hisoblagich (RAM-da)
ACTIVE_USERS = set()

@dp.message(Command("start"))
async def start_cmd(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    text = (
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "/new_resume - Yangi obektivka\n"
        "/help - Yordam\n\n"
        "@octagon_print"
    )
    await m.answer(text)

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("Savol bo‘lsa yozing: @octagon_print")

@dp.message(Command("new_resume"))
async def new_resume_cmd(m: Message):
    base = (APP_BASE or "").rstrip('/')  # <<— MUHIM
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
# FASTAPI APP + TEMPLATES
# =========================
app = FastAPI()

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
    """
    WebApp forma. templates/form.html ichida {{ tg_id }} ishlatiladi.
    """
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# =========================
# DOCX -> PDF (LibreOffice)
# =========================
def convert_docx_to_pdf(docx_bytes: bytes) -> bytes:
    """
    LibreOffice (soffice) orqali DOCX'ni PDF'ga aylantiradi.
    Dockerfile'da: libreoffice-common libreoffice-writer bo'lishi shart.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "resume.docx")
        pdf_path = os.path.join(tmpdir, "resume.pdf")

        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        # --headless konvert
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, docx_path],
            check=True
        )

        with open(pdf_path, "rb") as f:
            return f.read()

# =========================
# FORMA QABUL QILISH + FAYLLARNI YUBORISH
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
    # relatives JSON
    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

    # docxtpl kontekst
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

    # DOCX templatega yo'l
    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx template topilmadi"}, status_code=500)

    # DOCX generatsiya
    doc = DocxTemplate(tpl_path)

    inline_img = None
    if photo:
        img_bytes = await photo.read()
        # Rasmni InlineImage sifatida joylaymiz (eni ~35 mm, 3x4ga yaqin)
        try:
            inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
        except Exception:
            inline_img = None
    ctx["photo"] = inline_img

    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()

    # DOCX → PDF (LibreOffice)
    try:
        pdf_bytes = convert_docx_to_pdf(docx_bytes)
    except Exception as e:
        # Agar PDF yiqilsa ham DOCX jo'natamiz; xatoni qaytaramiz
        print("=== DOCX->PDF ERROR ===", file=sys.stderr)
        print(repr(e), file=sys.stderr)
        traceback.print_exc()
        pdf_bytes = None

    # Fayl nomlari
    safe_name = "_".join(full_name.split())
    docx_name = f"{safe_name}_0.docx"
    pdf_name  = f"{safe_name}_0.pdf"

       # Telegramga yuborish
    try:
        chat_id = int(tg_id)

        # DOCX (xotiradagi baytlar -> InputFile)
        docx_input = BufferedInputFile(docx_bytes, filename=docx_name)
        await bot.send_document(
            chat_id,
            document=docx_input,
            caption="✅ Word formatdagi rezyume"
        )

        # PDF bo'lsa, uni ham yuboramiz
        if pdf_bytes:
            pdf_input = BufferedInputFile(pdf_bytes, filename=pdf_name)
            await bot.send_document(
                chat_id,
                document=pdf_input,
                caption="✅ PDF formatdagi rezyume"
            )
        else:
            await bot.send_message(
                chat_id,
                "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi."
            )

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})


    return {"status": "success"}

# =========================
# WEBHOOK (XAVFSIZ)
# =========================
@app.post("/bot/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram webhook qabul qiluvchi endpoint.
    500 bo'lib navbat to'planmasligi uchun xatoda ham 200 qaytaramiz (ok=False).
    """
    data = await request.json()
    try:
        # Aiogram 3: agar feed_raw_update mavjud bo'lsa, uni ishlatamiz.
        if hasattr(dp, "feed_raw_update"):
            await dp.feed_raw_update(bot, data)  # raw JSON
        else:
            # Ba'zi versiyalarda raw mavjud bo'lmasligi mumkin:
            update = Update.model_validate(data)
            await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        # Xatoni Railway loglariga chiqaramiz
        print("=== WEBHOOK ERROR ===", file=sys.stderr)
        print(repr(e), file=sys.stderr)
        traceback.print_exc()
        print("Update JSON:", data, file=sys.stderr)
        # Baribir 200 qaytamiz — Telegram navbatni o‘tkazishi uchun
        return {"ok": False}

# =========================
# WEBHOOK O'RNATISH
# =========================
@app.get("/bot/set_webhook")
async def set_webhook(base: str | None = None):
    base_url = (base or APP_BASE).rstrip('/')   # <<— MUHIM
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}

# =========================
# DEBUG ENDPOINTLAR
# =========================
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}

@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}
