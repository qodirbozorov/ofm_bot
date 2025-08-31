# app/main.py
import os
import io
import re
import json
import sys
import asyncio
import logging
import tempfile
import aiofiles
from typing import Optional, List, Dict, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from fastapi import FastAPI, Request, Form, UploadFile, HTTPException
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
from pydantic import BaseModel, validator
from pydantic_settings import BaseSettings

# =========================
# KONFIGURATSIYA
# =========================
class Settings(BaseSettings):
    bot_token: str
    app_base: str = "https://ofmbot-production.up.railway.app"
    group_chat_id: int = -1003046464831
    templates_dir: str = os.path.join(os.path.dirname(__file__), "templates")
    
    class Config:
        env_file = ".env"

settings = Settings()
BOT_TOKEN = settings.bot_token
APP_BASE = settings.app_base
GROUP_CHAT_ID = settings.group_chat_id
TEMPLATES_DIR = settings.templates_dir

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# =========================
# MODELLAR
# =========================
class Relative(BaseModel):
    name: str
    relationship: str
    birth_year: str
    job: str
    address: str

class ResumeData(BaseModel):
    full_name: str
    phone: str
    tg_id: str
    birth_date: str = ""
    birth_place: str = ""
    nationality: str = "O‘zbek"
    party_membership: str = "Yo‘q"
    education: str = ""
    university: str = ""
    specialization: str = "Yo‘q"
    ilmiy_daraja: str = "Yo‘q"
    ilmiy_unvon: str = "Yo‘q"
    languages: str = "Yo‘q"
    dav_mukofoti: str = "Yo‘q"
    deputat: str = "Yo‘q"
    adresss: str = ""
    current_position_date: str = ""
    current_position_full: str = ""
    work_experience: str = ""
    relatives: List[Relative] = []
    
    @validator('phone')
    def validate_phone(cls, v):
        # Telefon raqamni tozalash va tekshirish
        cleaned = re.sub(r'[^\d+]', '', v)
        if not cleaned.startswith('+') and len(cleaned) >= 9:
            cleaned = '+998' + cleaned[-9:]
        return cleaned

# =========================
# AIROGRAM
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ACTIVE_USERS = set()
executor = ThreadPoolExecutor(max_workers=4)

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
    base = APP_BASE.rstrip("/")
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
# YORDAMCHI FUNKSIYALAR
# =========================
def make_safe_basename(full_name: str, phone: str) -> str:
    """Xavfsiz fayl nomi yaratish"""
    base = "_".join((full_name or "user").strip().split())
    base = re.sub(r"[^A-Za-z0-9_]+", "", base) or "user"
    ph = re.sub(r'[^\d]', '', phone)[-6:] or "NaN"
    dm = datetime.utcnow().strftime("%d-%m")
    return f"{base}_{ph}_{dm}".lower()

def pick_image_ext(upload_name: str | None) -> str:
    """Rasm fayl kengaytmasini aniqlash"""
    ext = (os.path.splitext(upload_name or "")[1] or "").lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ext
    return ".png"

async def convert_docx_to_pdf(docx_bytes: bytes) -> Optional[bytes]:
    """DOCX ni PDF ga aylantirish (LibreOffice)"""
    loop = asyncio.get_event_loop()
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, "resume.docx")
            pdf_path = os.path.join(tmpdir, "resume.pdf")
            
            # DOCX faylini yozish
            async with aiofiles.open(docx_path, "wb") as f:
                await f.write(docx_bytes)
            
            # PDF ga aylantirish
            def convert():
                try:
                    subprocess.run(
                        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, docx_path],
                        check=True, capture_output=True, timeout=30
                    )
                    with open(pdf_path, "rb") as f:
                        return f.read()
                except subprocess.TimeoutExpired:
                    logger.error("PDF conversion timeout")
                    return None
                except Exception as e:
                    logger.error(f"PDF conversion error: {e}")
                    return None
            
            # Bloklovchi operatsiyani threadda bajarish
            return await loop.run_in_executor(executor, convert)
    except Exception as e:
        logger.error(f"PDF conversion failed: {e}")
        return None

async def render_docx_template(context: Dict[str, Any], photo_bytes: Optional[bytes] = None) -> bytes:
    """DOCX shablonini render qilish"""
    loop = asyncio.get_event_loop()
    
    def render():
        try:
            tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
            if not os.path.exists(tpl_path):
                raise FileNotFoundError("resume.docx template topilmadi")
                
            doc = DocxTemplate(tpl_path)
            
            # Rasm qo'shish (agar mavjud bo'lsa)
            if photo_bytes:
                inline_img = InlineImage(doc, io.BytesIO(photo_bytes), width=Mm(35))
                context["photo"] = inline_img
            else:
                context["photo"] = None
                
            # Hujjatni yaratish
            buffer = io.BytesIO()
            doc.render(context)
            doc.save(buffer)
            return buffer.getvalue()
        except Exception as e:
            logger.error(f"Template rendering error: {e}")
            raise
    
    return await loop.run_in_executor(executor, render)

# =========================
# FASTAPI
# =========================
app = FastAPI(title="ResumeBot API", version="1.0.0")

# Jinja2 muhitini sozlash
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global xatoliklar handleri"""
    logger.error(f"Global error: {exc}", exc_info=True)
    
    # Clientga qaytarish
    error_msg = str(exc)
    if isinstance(exc, (ValueError, TypeError)):
        return JSONResponse({"status": "error", "error": "Noto'g'ri ma'lumot formati"}, status_code=400)
    elif isinstance(exc, FileNotFoundError):
        return JSONResponse({"status": "error", "error": "Shablon topilmadi"}, status_code=500)
    else:
        return JSONResponse({"status": "error", "error": "Server ichki xatosi"}, status_code=500)

@app.get("/", response_class=PlainTextResponse)
async def root():
    """Asosiy sahifa"""
    return "OK"

@app.get("/form", response_class=HTMLResponse)
async def get_form(id: str = ""):
    """Forma sahifasini ko'rsatish"""
    try:
        tpl = env.get_template("form.html")
        return tpl.render(tg_id=id)
    except Exception as e:
        logger.error(f"Form template error: {e}")
        raise HTTPException(status_code=500, detail="Forma sahifasi yuklanmadi")

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
    """Resume ma'lumotlarini qabul qilish va hujjat yaratish"""
    try:
        # 1) Ma'lumotlarni tekshirish va tozalash
        try:
            phone_cleaned = re.sub(r'[^\d+]', '', phone)
            if not phone_cleaned.startswith('+') and len(phone_cleaned) >= 9:
                phone_cleaned = '+998' + phone_cleaned[-9:]
        except:
            phone_cleaned = phone
            
        # 2) Qarindoshlar ma'lumotlari
        try:
            rels_data = json.loads(relatives) if relatives else []
            rels = [Relative(**item) for item in rels_data]
        except Exception as e:
            logger.warning(f"Relatives parsing error: {e}")
            rels = []

        # 3) Rasmni yuklash
        photo_bytes = None
        img_ext = ".png"
        if photo and photo.filename:
            try:
                photo_bytes = await photo.read()
                img_ext = pick_image_ext(photo.filename)
            except Exception as e:
                logger.error(f"Photo reading error: {e}")

        # 4) Kontekst yaratish
        context = {
            "full_name": full_name,
            "phone": phone_cleaned,
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
            "relatives": [rel.dict() for rel in rels],
        }

        # 5) DOCX yaratish
        docx_bytes = await render_docx_template(context, photo_bytes)
        
        # 6) PDF yaratish (async)
        pdf_bytes = await convert_docx_to_pdf(docx_bytes)

        # 7) Fayl nomlari
        base_name = make_safe_basename(full_name, phone_cleaned)
        docx_name = f"{base_name}.docx"
        pdf_name = f"{base_name}.pdf"
        img_name = f"{base_name}{img_ext}" if photo_bytes else None
        json_name = f"{base_name}.json"

        # 8) Guruhga ma'lumotlarni yuborish
        try:
            # a) Rasm (agar mavjud bo'lsa)
            if photo_bytes and img_name:
                await bot.send_document(
                    GROUP_CHAT_ID,
                    BufferedInputFile(photo_bytes, filename=img_name),
                    caption=f"🆕 Yangi forma: {full_name}\n📞 {phone_cleaned}\n👤 TG: {tg_id}"
                )

            # b) JSON ma'lumotlar
            payload = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "tg_id": tg_id,
                "full_name": full_name,
                "phone": phone_cleaned,
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
                "relatives": [rel.dict() for rel in rels],
            }
            
            json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(json_bytes, filename=json_name),
                caption=f"📄 Ma'lumotlar JSON: {full_name}"
            )
        except Exception as e:
            logger.error(f"Group send error: {e}")

        # 9) Foydalanuvchiga hujjatlarni yuborish
        try:
            chat_id = int(tg_id)
            # DOCX yuborish
            await bot.send_document(
                chat_id,
                BufferedInputFile(docx_bytes, filename=docx_name),
                caption="✅ Word formatdagi rezyume"
            )
            
            # PDF yuborish (agar mavjud bo'lsa)
            if pdf_bytes:
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(pdf_bytes, filename=pdf_name),
                    caption="✅ PDF formatdagi rezyume"
                )
            else:
                await bot.send_message(
                    chat_id, 
                    "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi. " +
                    "Iltimos, Word hujjatini PDF ga o'zingiz aylantiring."
                )
        except Exception as e:
            logger.error(f"User send error: {e}")
            return JSONResponse(
                {"status": "error", "error": f"Hujjat yuborishda xatolik: {str(e)}"}, 
                status_code=200
            )

        return {"status": "success", "message": "Hujjatlar muvaffaqiyatli yuborildi"}
        
    except Exception as e:
        logger.error(f"Resume processing error: {e}", exc_info=True)
        return JSONResponse(
            {"status": "error", "error": f"Server ichki xatosi: {str(e)}"}, 
            status_code=200
        )

# =========================
# WEBHOOK
# =========================
@app.post("/bot/webhook")
async def telegram_webhook(request: Request):
    """Telegram webhook handler"""
    try:
        data = await request.json()
        if hasattr(dp, "feed_raw_update"):
            await dp.feed_raw_update(bot, data)
        else:
            update = Update.model_validate(data)
            await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}

@app.get("/bot/set_webhook")
async def set_webhook():
    """Webhook ni sozlash"""
    base_url = APP_BASE.rstrip("/")
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}

# =========================
# DEBUG
# =========================
@app.get("/debug/ping")
async def debug_ping():
    """Server holatini tekshirish"""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/debug/getme")
async def debug_getme():
    """Bot ma'lumotlarini olish"""
    try:
        me = await bot.get_me()
        return {"id": me.id, "username": me.username, "first_name": me.first_name}
    except Exception as e:
        logger.error(f"Getme error: {e}")
        return {"error": str(e)}

@app.get("/debug/stats")
async def debug_stats():
    """Bot statistikasi"""
    return {
        "active_users": len(ACTIVE_USERS),
        "timestamp": datetime.utcnow().isoformat(),
        "webhook": f"{APP_BASE.rstrip('/')}/bot/webhook"
    }

# =========================
# STARTUP/SHUTDOWN
# =========================
@app.on_event("startup")
async def startup_event():
    """Dastur ishga tushganda"""
    logger.info("Starting up...")
    # Webhook ni sozlash
    base_url = APP_BASE.rstrip("/")
    await bot.set_webhook(f"{base_url}/bot/webhook")
    logger.info(f"Webhook set to: {base_url}/bot/webhook")

@app.on_event("shutdown")
async def shutdown_event():
    """Dastur to'xtatilganda"""
    logger.info("Shutting down...")
    # Executorni to'xtatish
    executor.shutdown(wait=False)
    # Bot sessiyasini yopish
    await bot.session.close()