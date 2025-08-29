# app/main.py
import os
import io
import json
import sys
import subprocess
import tempfile
import traceback
from typing import Optional

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
    BufferedInputFile
)

# =========================
# CONFIG (ENV bilan)
# =========================
BOT_TOKEN: str = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE: str = os.getenv("APP_BASE", "https://ofmbot-production.up.railway.app").rstrip("/")
DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")  # postgresql+asyncpg://USER:PASS@HOST:PORT/DB

# =========================
# SQLAlchemy (async) setup
# =========================
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, Text, LargeBinary, ForeignKey, DateTime, func, text

class Base(DeclarativeBase):
    pass

class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[str] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(256))
    phone: Mapped[str] = mapped_column(String(64))
    birth_date: Mapped[str] = mapped_column(String(64), default="")
    birth_place: Mapped[str] = mapped_column(String(256), default="")
    nationality: Mapped[str] = mapped_column(String(64), default="")
    party_membership: Mapped[str] = mapped_column(String(128), default="")
    education: Mapped[str] = mapped_column(String(128), default="")
    university: Mapped[str] = mapped_column(String(256), default="")
    specialization: Mapped[str] = mapped_column(String(256), default="")
    ilmiy_daraja: Mapped[str] = mapped_column(String(256), default="")
    ilmiy_unvon: Mapped[str] = mapped_column(String(256), default="")
    languages: Mapped[str] = mapped_column(String(256), default="")
    dav_mukofoti: Mapped[str] = mapped_column(String(256), default="")
    deputat: Mapped[str] = mapped_column(String(256), default="")
    adresss: Mapped[str] = mapped_column(String(512), default="")
    current_position_date: Mapped[str] = mapped_column(String(256), default="")
    current_position_full: Mapped[str] = mapped_column(String(512), default="")
    work_experience: Mapped[str] = mapped_column(Text, default="")
    photo_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped["DateTime"] = mapped_column(DateTime(timezone=True), server_default=func.now())

    relatives: Mapped[list["Relative"]] = relationship(back_populates="submission", cascade="all, delete-orphan")

class Relative(Base):
    __tablename__ = "relatives"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    relation_type: Mapped[str] = mapped_column(String(64), default="")
    full_name: Mapped[str] = mapped_column(String(256), default="")
    b_year_place: Mapped[str] = mapped_column(String(256), default="")
    job_title: Mapped[str] = mapped_column(String(256), default="")
    address: Mapped[str] = mapped_column(String(512), default="")
    submission: Mapped["Submission"] = relationship(back_populates="relatives")

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True) if DATABASE_URL else None
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine else None

# =========================
# FastAPI app + templates
# =========================
app = FastAPI()

# Global exception handler — front doim JSON oladi (500 o‘rniga)
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print("=== GLOBAL ERROR ===", file=sys.stderr)
    print(repr(exc), file=sys.stderr)
    traceback.print_exc()
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)

# Templates
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

# =========================
# Aiogram bot
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
        "@octagon_print"
    )
    await m.answer(text)

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("Savol bo‘lsa yozing: @octagon_print")

@dp.message(Command("new_resume"))
async def new_resume_cmd(m: Message):
    base = (APP_BASE or "").rstrip('/')
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
# Startup: DB jadvallarini yaratish
# =========================
@app.on_event("startup")
async def on_startup():
    if engine:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            print("DB: tables ready", file=sys.stderr)
        except Exception as e:
            print("DB INIT ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
    else:
        print("⚠️ DATABASE_URL topilmadi — DB o‘chirilgan rejimda.", file=sys.stderr)

# =========================
# HTTP endpoints
# =========================
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# =========================
# DOCX -> PDF (LibreOffice)
# =========================
def convert_docx_to_pdf(docx_bytes: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "resume.docx")
        pdf_path = os.path.join(tmpdir, "resume.pdf")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, docx_path],
            check=True
        )
        with open(pdf_path, "rb") as f:
            return f.read()

# =========================
# Form data qabul qilish
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
    # relatives JSON parse
    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

    # Template yo‘li
    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx template topilmadi"}, status_code=200)

    # DOCX render context
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

    # DOCX generatsiya + rasm ixtiyoriy
    doc = DocxTemplate(tpl_path)

    raw_photo_bytes = None
    inline_img = None
    try:
        if photo is not None and getattr(photo, "filename", ""):
            img_bytes = await photo.read()
            if img_bytes:
                raw_photo_bytes = img_bytes  # DB uchun
                inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
    except Exception as e:
        print("PHOTO ERROR:", repr(e), file=sys.stderr)
        inline_img = None

    ctx["photo"] = inline_img

    # DOCX render & bytes
    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()

    # --- DB saqlash (bor bo‘lsa) ---
    if AsyncSessionLocal:
        try:
            async with AsyncSessionLocal() as session:
                sub = Submission(
                    tg_id=tg_id,
                    full_name=full_name,
                    phone=phone,
                    birth_date=birth_date,
                    birth_place=birth_place,
                    nationality=nationality,
                    party_membership=party_membership,
                    education=education,
                    university=university,
                    specialization=specialization,
                    ilmiy_daraja=ilmiy_daraja,
                    ilmiy_unvon=ilmiy_unvon,
                    languages=languages,
                    dav_mukofoti=dav_mukofoti,
                    deputat=deputat,
                    adresss=adresss,
                    current_position_date=current_position_date,
                    current_position_full=current_position_full,
                    work_experience=work_experience,
                    photo_bytes=raw_photo_bytes
                )
                for r in rels:
                    sub.relatives.append(Relative(
                        relation_type=r.get("relation_type", ""),
                        full_name=r.get("full_name", ""),
                        b_year_place=r.get("b_year_place", ""),
                        job_title=r.get("job_title", ""),
                        address=r.get("address", ""),
                    ))
                session.add(sub)
                await session.commit()
        except Exception as e:
            print("DB SAVE ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            # DB xato bo‘lsa ham oqim davom etadi

    # DOCX → PDF (LibreOffice)
    try:
        pdf_bytes = convert_docx_to_pdf(docx_bytes)
    except Exception as e:
        print("=== DOCX->PDF ERROR ===", file=sys.stderr)
        print(repr(e), file=sys.stderr)
        traceback.print_exc()
        pdf_bytes = None

    # Fayl nomlari
    safe_name = "_".join(full_name.split())
    docx_name = f"{safe_name}_0.docx"
    pdf_name  = f"{safe_name}_0.pdf"

    # Telegramga yuborish (BufferedInputFile)
    try:
        chat_id = int(tg_id)

        docx_input = BufferedInputFile(docx_bytes, filename=docx_name)
        await bot.send_document(chat_id, document=docx_input, caption="✅ Word formatdagi rezyume")

        if pdf_bytes:
            pdf_input = BufferedInputFile(pdf_bytes, filename=pdf_name)
            await bot.send_document(chat_id, document=pdf_input, caption="✅ PDF formatdagi rezyume")
        else:
            await bot.send_message(chat_id, "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi.")

    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=200)

    return {"status": "success"}

# =========================
# Telegram webhook
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
    base_url = (base or APP_BASE).rstrip('/')
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}

# =========================
# Debug endpoints
# =========================
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}

@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}

@app.get("/debug/db_status")
async def db_status():
    if not AsyncSessionLocal:
        return {"db": "disabled (DATABASE_URL yo‘q)"}
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        return {"db": "ok"}
    except Exception as e:
        return {"db": "error", "detail": str(e)}
