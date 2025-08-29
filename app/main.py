import os, io, json, tempfile, subprocess
from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm
from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update
from aiogram.filters import Command

# ===== CONFIG =====
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = os.getenv("APP_BASE", "ofmbot-production.up.railway.app")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ACTIVE_USERS = set()

# ===== BOT HANDLERS =====
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
    ACTIVE_USERS.add(m.from_user.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Obyektivkani to‘ldirish",
                web_app=WebAppInfo(url=f"{APP_BASE}/form?id={m.from_user.id}")
            )
        ]]
    )
    txt = ("👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
           "✅ Tez\n✅ Oson\n✅ Ishonchli\n"
           "quyidagi 🌐 web formani to'ldiring\n👇👇👇👇👇👇👇👇👇")
    await m.answer(txt, reply_markup=kb)

# ===== FASTAPI APP =====
app = FastAPI()
env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# === DOCX -> PDF helper ===
def convert_docx_to_pdf(docx_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "resume.docx")
        pdf_path = os.path.join(tmpdir, "resume.pdf")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        subprocess.run([
            "soffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, docx_path
        ], check=True)
        with open(pdf_path, "rb") as f:
            return f.read()

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
    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

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

    # DOCX generatsiya
    tpl_path = os.path.join(os.path.dirname(__file__), "templates", "resume.docx")
    doc = DocxTemplate(tpl_path)

    inline_img = None
    if photo:
        img_bytes = await photo.read()
        try:
            inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
        except:
            inline_img = None
    ctx["photo"] = inline_img

    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()

    # DOCX → PDF
    pdf_bytes = convert_docx_to_pdf(docx_bytes)

    # Fayl nomlari
    safe_name = "_".join(full_name.split())
    docx_name = f"{safe_name}_0.docx"
    pdf_name = f"{safe_name}_0.pdf"

    try:
        chat_id = int(tg_id)
        await bot.send_document(chat_id, document=(docx_name, io.BytesIO(docx_bytes)), caption="✅ Word formatdagi rezyume")
        await bot.send_document(chat_id, document=(pdf_name, io.BytesIO(pdf_bytes)), caption="✅ PDF formatdagi rezyume")
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)})

    return {"status": "success"}

# ===== TELEGRAM WEBHOOK =====
@app.post("/bot/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await dp.feed_raw_update(bot, data)
    return {"ok": True}

@app.get("/bot/set_webhook")
async def set_webhook(base: str | None = None):
    base_url = base or APP_BASE
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}
