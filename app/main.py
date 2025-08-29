import os, io, json, pdfkit
from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update

# ====== CONFIG (env shart emas dedingiz, shu yerga qo'ydim) ======
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = os.getenv("APP_BASE", "https://your-railway-app.up.railway.app")

# ====== BOT ======
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

# ====== WEB APP (FastAPI) ======
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

    # === DOCX: render with docxtpl ===
    tpl_path = os.path.join(os.path.dirname(__file__), "templates", "resume.docx")
    doc = DocxTemplate(tpl_path)

    inline_img = None
    if photo is not None:
        img_bytes = await photo.read()
        try:
            # If image is valid, add as InlineImage (35mm width ~ 3x4 photo)
            inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
        except Exception:
            inline_img = None
    ctx["photo"] = inline_img

    docx_buf = io.BytesIO()
    doc.render(ctx)
    doc.save(docx_buf)
    docx_buf.seek(0)

    # === PDF: render HTML -> PDF via wkhtmltopdf (pdfkit) ===
    html_tpl = env.get_template("resume.html")
    html_str = html_tpl.render(ctx)
    pdf_buf = io.BytesIO(pdfkit.from_string(html_str, False))

    # file names
    safe_name = "_".join(full_name.split())
    docx_name = f"{safe_name}_0.docx"
    pdf_name  = f"{safe_name}_0.pdf"

    # send to Telegram chat
    try:
        chat_id = int(tg_id)
        await bot.send_document(chat_id, document=(docx_name, docx_buf), caption="✅ Word formatdagi rezyume")
        await bot.send_document(chat_id, document=(pdf_name, pdf_buf), caption="✅ PDF formatdagi rezyume")
    except Exception as e:
        return JSONResponse({"status":"error", "error": str(e)})

    return {"status": "success"}

# ====== TELEGRAM WEBHOOK ======
@app.post("/bot/webhook")
async def telegram_webhook(request: Request):
    update = Update.model_validate(await request.json())
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/bot/set_webhook")
async def set_webhook(base: str | None = None):
    # Use provided ?base=... or APP_BASE
    base_url = base or APP_BASE
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}
