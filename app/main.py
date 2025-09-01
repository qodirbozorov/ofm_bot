# app/main.py
import os
import io
import re
import sys
import json
import tempfile
import traceback
import subprocess
from typing import Optional, Dict, Any, List
from datetime import datetime

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, Update, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"
GROUP_CHAT_ID = -1003046464831

MAX_FILE_MB = 10
MAX_FILE_SIZE = MAX_FILE_MB * 1024 * 1024

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ACTIVE_USERS = set()

# RAM session
SESS: Dict[int, Dict[str, Any]] = {}


def get_session(uid: int) -> Optional[Dict[str, Any]]:
    return SESS.get(uid)


def new_session(uid: int, op: str):
    SESS[uid] = {"op": op, "files": [], "params": {}}


def clear_session(uid: int):
    SESS.pop(uid, None)


def human_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    if n < 1024**2: return f"{n/1024:.1f} KB"
    if n < 1024**3: return f"{n/1024**2:.1f} MB"
    return f"{n/1024**3:.1f} GB"


# =========================
# FASTAPI + Templates
# =========================
app = FastAPI()
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATES_DIR),
                  autoescape=select_autoescape(["html", "xml"]))


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print("=== GLOBAL ERROR ===", file=sys.stderr)
    print(repr(exc), file=sys.stderr)
    traceback.print_exc()
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)


@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"


@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)


# =========================
# Helpers
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


def convert_docx_to_pdf(docx_bytes: bytes) -> Optional[bytes]:
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.docx")
        out_path = os.path.join(td, "in.pdf")
        with open(in_path, "wb") as f:
            f.write(docx_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, in_path],
                check=True
            )
            with open(out_path, "rb") as f:
                return f.read()
        except Exception as e:
            print("DOCX->PDF ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None


def libre_convert(input_bytes: bytes, out_ext: str, in_name: str) -> Optional[bytes]:
    """
    Generic LibreOffice convert: (docx|pptx|xlsx|pdf) -> out_ext
    """
    with tempfile.TemporaryDirectory() as td:
        base = os.path.basename(in_name) or "in.bin"
        in_path = os.path.join(td, base)
        with open(in_path, "wb") as f:
            f.write(input_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", out_ext, "--outdir", td, in_path],
                check=True
            )
            out_path = None
            for fn in os.listdir(td):
                if fn.lower().endswith(f".{out_ext}"):
                    out_path = os.path.join(td, fn)
                    break
            if not out_path:
                return None
            with open(out_path, "rb") as f:
                return f.read()
        except Exception as e:
            print("LIBRE CONVERT ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None


# PDF helpers
def pdf_split_bytes(pdf_bytes: bytes, range_str: str) -> Optional[bytes]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        total = len(reader.pages)

        wanted: List[int] = []
        for chunk in re.split(r"[,\s]+", (range_str or "").strip()):
            if not chunk:
                continue
            if "-" in chunk:
                a, b = chunk.split("-", 1)
                a = max(1, int(a))
                b = min(total, int(b))
                if a <= b:
                    wanted.extend(range(a, b + 1))
            else:
                p = int(chunk)
                if 1 <= p <= total:
                    wanted.append(p)

        if not wanted:
            return None

        for p in wanted:
            writer.add_page(reader.pages[p - 1])

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:
        print("PDF SPLIT ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


def pdf_merge_bytes(files: List[bytes]) -> Optional[bytes]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
        writer = PdfWriter()
        for b in files:
            r = PdfReader(io.BytesIO(b))
            for pg in r.pages:
                writer.add_page(pg)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:
        print("PDF MERGE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


def pdf_overlay_text(pdf_bytes: bytes, text: str, pos: str = "bottom-right",
                     font_size: int = 10) -> Optional[bytes]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()

        for i, page in enumerate(reader.pages, start=1):
            media = page.mediabox
            w = float(media.width)
            h = float(media.height)

            packet = io.BytesIO()
            c = canvas.Canvas(packet, pagesize=(w, h))
            c.setFont("Helvetica", font_size)

            txt = text.replace("{page}", str(i))

            margin = 20
            tw = c.stringWidth(txt, "Helvetica", font_size)
            th = font_size + 2

            x, y = margin, margin
            if "top" in pos:
                y = h - th - margin
            if "bottom" in pos:
                y = margin
            if "right" in pos:
                x = w - tw - margin
            if "left" in pos:
                x = margin
            if "center" in pos:
                x = (w - tw) / 2

            c.drawString(x, y, txt)
            c.save()

            packet.seek(0)
            overlay = PdfReader(packet).pages[0]
            page.merge_page(overlay)
            writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:
        print("PDF OVERLAY ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


def ocr_pdf_to_text(pdf_bytes: bytes, lang: str = "eng") -> str:
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
        images = convert_from_bytes(pdf_bytes, dpi=200)
        texts = [pytesseract.image_to_string(img, lang=lang) for img in images]
        return "\n\n".join(texts).strip()
    except Exception as e:
        print("OCR ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return ""


# Images → PDF page, and make any input become PDF bytes
def image_to_pdf_page(img_bytes: bytes) -> Optional[bytes]:
    try:
        from PIL import Image
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader

        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(w, h))
        c.drawImage(ImageReader(img), 0, 0, width=w, height=h,
                    preserveAspectRatio=True, anchor='sw')
        c.showPage()
        c.save()
        return packet.getvalue()
    except Exception as e:
        print("IMG->PDF ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


def ensure_pdf_bytes(name: str, data: bytes, mime: str) -> Optional[bytes]:
    ext = (os.path.splitext(name.lower())[1] or "")
    if mime == "application/pdf" or ext == ".pdf":
        return data
    if mime in {"image/jpeg", "image/png", "image/webp"} or ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return image_to_pdf_page(data)
    return None


# =========================
# WebApp (Resume) — 422 yo‘q, bo‘sh ham bo‘lsa ishlaydi
# =========================
@app.post("/send_resume_data")
async def send_resume_data(
    full_name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    tg_id: Optional[str] = Form(None),

    birth_date: Optional[str] = Form(None),
    birth_place: Optional[str] = Form(None),
    nationality: Optional[str] = Form(None),
    party_membership: Optional[str] = Form(None),
    education: Optional[str] = Form(None),
    university: Optional[str] = Form(None),
    specialization: Optional[str] = Form(None),
    ilmiy_daraja: Optional[str] = Form(None),
    ilmiy_unvon: Optional[str] = Form(None),
    languages: Optional[str] = Form(None),
    dav_mukofoti: Optional[str] = Form(None),
    deputat: Optional[str] = Form(None),
    adresss: Optional[str] = Form(None),
    current_position_date: Optional[str] = Form(None),
    current_position_full: Optional[str] = Form(None),
    work_experience: Optional[str] = Form(None),
    relatives: Optional[str] = Form(None),

    photo: UploadFile | None = None,  # WebApp rasmiga limit qo‘ymaymiz
):
    def nz(v, default=""):
        return v if v is not None else default

    full_name = nz(full_name)
    phone = nz(phone)
    tg_id_str = nz(tg_id)

    birth_date = nz(birth_date)
    birth_place = nz(birth_place)
    nationality = nz(nationality, "O‘zbek")
    party_membership = nz(party_membership, "Yo‘q")
    education = nz(education)
    university = nz(university)
    specialization = nz(specialization, "Yo‘q")
    ilmiy_daraja = nz(ilmiy_daraja, "Yo‘q")
    ilmiy_unvon = nz(ilmiy_unvon, "Yo‘q")
    languages = nz(languages, "Yo‘q")
    dav_mukofoti = nz(dav_mukofoti, "Yo‘q")
    deputat = nz(deputat, "Yo‘q")
    adresss = nz(adresss)
    current_position_date = nz(current_position_date)
    current_position_full = nz(current_position_full)
    work_experience = nz(work_experience)

    try:
        rels = json.loads(relatives) if relatives else []
        if not isinstance(rels, list):
            rels = []
    except Exception:
        rels = []

    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx template topilmadi"}, status_code=200)

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

    doc = DocxTemplate(tpl_path)

    inline_img = None
    img_bytes = None
    img_ext = ".png"
    try:
        if photo is not None and getattr(photo, "filename", ""):
            img_bytes = await photo.read()
            img_ext = pick_image_ext(photo.filename)
            if img_bytes:
                inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
    except Exception as e:
        print("PHOTO INLINE ERROR:", repr(e), file=sys.stderr)

    ctx["photo"] = inline_img

    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()
    pdf_bytes = convert_docx_to_pdf(docx_bytes)

    base_name = make_safe_basename(full_name or "user", phone or "NaN")
    docx_name = f"{base_name}_0.docx"
    pdf_name = f"{base_name}_0.pdf"
    img_name = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # Guruhga: rasm + JSON (har doim ok)
    try:
        if img_bytes:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img_bytes, filename=img_name),
                caption=f"🆕 Forma: {full_name or '—'}\n📞 {phone or '—'}\n👤 TG: {tg_id_str or '—'}"
            )
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "tg_id": tg_id_str, "full_name": full_name, "phone": phone,
            "birth_date": birth_date, "birth_place": birth_place,
            "nationality": nationality, "party_membership": party_membership,
            "education": education, "university": university, "specialization": specialization,
            "ilmiy_daraja": ilmiy_daraja, "ilmiy_unvon": ilmiy_unvon, "languages": languages,
            "dav_mukofoti": dav_mukofoti, "deputat": deputat, "adresss": adresss,
            "current_position_date": current_position_date, "current_position_full": current_position_full,
            "work_experience": work_experience, "relatives": rels,
        }
        jb = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await bot.send_document(
            GROUP_CHAT_ID,
            BufferedInputFile(jb, filename=json_name),
            caption=f"📄 JSON: {full_name or '—'}"
        )
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()

    # Foydalanuvchiga
    try:
        chat_id = int(tg_id_str) if tg_id_str.strip() else None
    except Exception:
        chat_id = None

    if chat_id:
        try:
            await bot.send_document(chat_id, BufferedInputFile(docx_bytes, filename=docx_name),
                                    caption="✅ Word formatdagi rezyume")
            if pdf_bytes:
                await bot.send_document(chat_id, BufferedInputFile(pdf_bytes, filename=pdf_name),
                                        caption="✅ PDF formatdagi rezyume")
            else:
                await bot.send_message(chat_id, "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi.")
        except Exception as e:
            print("USER SEND ERROR:", repr(e), file=sys.stderr)

    return {"status": "success", "close": True}


# =========================
# Reply Keyboards (bottom)
# =========================
BTN_NEW = "🧾 Yangi Rezyume"
BTN_SPLIT = "✂️ PDF Split"
BTN_MERGE = "🧷 PDF Merge"
BTN_PAGENUM = "🔢 Page Numbers"
BTN_WM = "💧 Watermark"
BTN_OCR = "🪄 OCR"
BTN_CONVERT = "🔁 Convert"
BTN_TRANSLATE = "🌐 Translate"
BTN_HELP = "ℹ️ Help"
BTN_CANCEL = "❌ Cancel"
BTN_BACK = "↩️ Back"
BTN_DONE = "✅ Yakunlash"

# Param buttons
BTN_SET_RANGE = "🧭 Set Range"
BTN_SET_WM_TEXT = "📝 Set Text"
BTN_SET_LANG = "🌍 Set Language"
BTN_SET_TRG_LANG = "🌐 Set Target Lang"
BTN_TARGET_PDF = "🎯 Target: PDF"
BTN_TARGET_PNG = "🎯 Target: PNG"
BTN_TARGET_DOCX = "🎯 Target: DOCX"
BTN_TARGET_PPTX = "🎯 Target: PPTX"

# Positions
BTN_TL = "↖️ Top-Left"; BTN_TC = "⬆️ Top-Center"; BTN_TR = "↗️ Top-Right"
BTN_BL = "↙️ Bottom-Left"; BTN_BC = "⬇️ Bottom-Center"; BTN_BR = "↘️ Bottom-Right"


def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_SPLIT), KeyboardButton(text=BTN_MERGE)],
            [KeyboardButton(text=BTN_PAGENUM), KeyboardButton(text=BTN_WM), KeyboardButton(text=BTN_OCR)],
            [KeyboardButton(text=BTN_CONVERT), KeyboardButton(text=BTN_TRANSLATE)],
            [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Funksiyani tanlang…",
        one_time_keyboard=False
    )


def kb_split() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_RANGE), KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


def kb_merge() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


def kb_pagenum() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TL), KeyboardButton(text=BTN_TC), KeyboardButton(text=BTN_TR)],
            [KeyboardButton(text=BTN_BL), KeyboardButton(text=BTN_BC), KeyboardButton(text=BTN_BR)],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


def kb_watermark() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_WM_TEXT)],
            [KeyboardButton(text=BTN_TL), KeyboardButton(text=BTN_TC), KeyboardButton(text=BTN_TR)],
            [KeyboardButton(text=BTN_BL), KeyboardButton(text=BTN_BC), KeyboardButton(text=BTN_BR)],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


def kb_ocr() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_LANG)],
            [KeyboardButton(text="eng"), KeyboardButton(text="uzb"), KeyboardButton(text="rus")],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


def kb_convert() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TARGET_PDF), KeyboardButton(text=BTN_TARGET_PNG)],
            [KeyboardButton(text=BTN_TARGET_DOCX), KeyboardButton(text=BTN_TARGET_PPTX)],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


def kb_translate() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_TRG_LANG)],
            [KeyboardButton(text="uz"), KeyboardButton(text="ru"), KeyboardButton(text="en")],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )


# =========================
# FSM States (free text inputs)
# =========================
class SplitRangeSG(StatesGroup):
    waiting = State()


class WMTextSG(StatesGroup):
    waiting = State()


class LangSG(StatesGroup):
    waiting = State()


class TargetLangSG(StatesGroup):
    waiting = State()


# =========================
# Bot: Menu & flows
# =========================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    await m.answer(
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "Quyidagi menyudan funksiyani tanlang. Fayl yuborishda limit: 10 MB.",
        reply_markup=kb_main()
    )


@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "📌 Qisqa qo‘llanma:\n"
        "• 🧾 Yangi Rezyume: Web formani ochadi va tayyor DOCX/PDF ni yuboradi.\n"
        "• ✂️ Split: PDF yuboring → Range berasiz → Yakunlash.\n"
        "• 🧷 Merge: Bir nechta PDF yuboring → Yakunlash.\n"
        "• 🔢 Page Numbers: PDF yuboring → joylashuvni tanlang → Yakunlash.\n"
        "• 💧 Watermark: PDF yuboring → matn va joylashuv → Yakunlash.\n"
        "• 🪄 OCR: Skan PDF yuboring → til tanlang → Yakunlash.\n"
        "• 🔁 Convert: JPG/PNG/PDF ko‘p faylni bir PDFga yoki Office → PDF, PPTX/PDF → PNG.\n"
        "• 🌐 Translate: PDFdan text olib maqsad tilga tarjima.\n"
        "Bekor qilish: ❌ Cancel, Ortga: ↩️ Back",
        reply_markup=kb_main()
    )


@dp.message(F.text == BTN_NEW)
@dp.message(Command("new_resume"))
async def new_resume_cmd(m: Message):
    base = (APP_BASE or "").rstrip("/")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🌐 Obyektivkani to‘ldirish",
            web_app=WebAppInfo(url=f"{base}/form?id={m.from_user.id}")
        )
    ]])
    await m.answer(
        "👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
        "✅ Tez\n✅ Oson\n✅ Ishonchli\nQuyidagi web formani to‘ldiring:",
        reply_markup=kb
    )


@dp.message(F.text == BTN_CANCEL)
async def cancel_flow(m: Message, state: FSMContext):
    await state.clear()
    clear_session(m.from_user.id)
    await m.answer("❌ Jarayon bekor qilindi.", reply_markup=kb_main())


@dp.message(F.text == BTN_BACK)
async def back_to_menu(m: Message, state: FSMContext):
    await state.clear()
    clear_session(m.from_user.id)
    await m.answer("↩️ Asosiy menyu.", reply_markup=kb_main())


@dp.message(F.text == BTN_SPLIT)
async def flow_split(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "split")
    await m.answer(
        "✂️ PDF Split boshlandi.\n1) PDF yuboring (max 10 MB)\n2) 🧭 Set Range (masalan: 1-3,7)\n3) ✅ Yakunlash",
        reply_markup=kb_split()
    )


@dp.message(F.text == BTN_MERGE)
async def flow_merge(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "merge")
    await m.answer(
        "🧷 PDF Merge boshlandi.\nKetma-ket PDF yuboring (har biri max 10 MB), so‘ng ✅ Yakunlash.",
        reply_markup=kb_merge()
    )


@dp.message(F.text == BTN_PAGENUM)
async def flow_pagenum(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "pagenum")
    await m.answer(
        "🔢 Sahifa raqami sessiyasi.\n1) PDF yuboring (max 10 MB)\n2) Joylashuvni tanlang\n3) ✅ Yakunlash",
        reply_markup=kb_pagenum()
    )


@dp.message(F.text == BTN_WM)
async def flow_wm(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "watermark")
    await m.answer(
        "💧 Watermark sessiyasi.\n1) PDF yuboring (max 10 MB)\n2) 📝 Set Text\n3) Joylashuvni tanlang\n4) ✅ Yakunlash",
        reply_markup=kb_watermark()
    )


@dp.message(F.text == BTN_OCR)
async def flow_ocr(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "ocr")
    await m.answer(
        "🪄 OCR sessiyasi.\n1) Skan PDF yuboring (max 10 MB)\n2) 🌍 Set Language (eng/uzb/rus)\n3) ✅ Yakunlash",
        reply_markup=kb_ocr()
    )


@dp.message(F.text == BTN_CONVERT)
async def flow_convert(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "convert")
    await m.answer(
        "🔁 Convert sessiyasi.\n"
        "• Ko‘p JPG/PNG/PDF yuborsangiz → 🎯 Target: PDF → ✅ Yakunlash (hammasi bitta PDF bo‘ladi).\n"
        "• Yagona DOCX/PPTX/XLSX yuborsangiz → 🎯 Target: PDF → ✅ Yakunlash.\n"
        "• PPTX/PDF → 🎯 Target: PNG → ✅ Yakunlash (1-sahifa/slayd).\n"
        "Fayl yuboring (har biri max 10 MB), so‘ng targetni tanlang.",
        reply_markup=kb_convert()
    )


@dp.message(F.text == BTN_TRANSLATE)
async def flow_translate(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "translate")
    await m.answer(
        "🌐 Tarjima sessiyasi.\n1) PDF yuboring (max 10 MB)\n2) 🌐 Set Target Lang (masalan: uz/ru/en)\n3) ✅ Yakunlash",
        reply_markup=kb_translate()
    )


@dp.message(F.text == BTN_SET_RANGE)
async def ask_range(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "split":
        return await m.answer("Bu parametr Split sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(SplitRangeSG.waiting)
    await m.answer("Oraliq kiriting (masalan: 1-3,7):")


@dp.message(SplitRangeSG.waiting, F.text)
async def got_range(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:  # ehtiyot
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    s["params"]["range"] = (m.text or "").strip()
    await state.clear()
    await m.answer("✅ Oraliq qabul qilindi.", reply_markup=kb_split())


@dp.message(F.text == BTN_SET_WM_TEXT)
async def ask_wm(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "watermark":
        return await m.answer("Bu parametr Watermark sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(WMTextSG.waiting)
    await m.answer("Watermark matnini kiriting (masalan: Confidential):")


@dp.message(WMTextSG.waiting, F.text)
async def got_wm(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    txt = (m.text or "").strip()
    if not txt:
        return await m.answer("Matn bo‘sh bo‘lmasin.")
    s["params"]["wm"] = txt[:100]
    await state.clear()
    await m.answer("✅ Watermark matni qabul qilindi.", reply_markup=kb_watermark())


# Pozitsiya tanlovlari (pagenum va watermark)
POS_MAP = {
    BTN_TL: "top-left", BTN_TC: "top-center", BTN_TR: "top-right",
    BTN_BL: "bottom-left", BTN_BC: "bottom-center", BTN_BR: "bottom-right",
}

@dp.message(F.text.in_(list(POS_MAP.keys())))
async def set_position(m: Message):
    s = get_session(m.from_user.id)
    if not s or s["op"] not in {"pagenum", "watermark"}:
        return await m.answer("Joylashuv tanlash bu sessiyada emas.", reply_markup=kb_main())
    s["params"]["pos"] = POS_MAP[m.text]
    await m.answer(f"✅ Pozitsiya: {POS_MAP[m.text]}", reply_markup=kb_pagenum() if s["op"]=="pagenum" else kb_watermark())


@dp.message(F.text == BTN_SET_LANG)
async def ask_lang(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "ocr":
        return await m.answer("Bu parametr OCR sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(LangSG.waiting)
    await m.answer("Til kiriting (masalan: eng, uzb, rus):", reply_markup=kb_ocr())


@dp.message(LangSG.waiting, F.text)
async def got_lang(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    s["params"]["lang"] = (m.text or "").strip()
    await state.clear()
    await m.answer("✅ Til qabul qilindi.", reply_markup=kb_ocr())


@dp.message(F.text == BTN_SET_TRG_LANG)
async def ask_to_lang(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "translate":
        return await m.answer("Bu parametr Translate sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(TargetLangSG.waiting)
    await m.answer("Maqsad til kodini kiriting (uz/ru/en ...):", reply_markup=kb_translate())


@dp.message(TargetLangSG.waiting, F.text)
async def got_to_lang(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    s["params"]["to"] = (m.text or "").strip()
    await state.clear()
    await m.answer("✅ Maqsad til qabul qilindi.", reply_markup=kb_translate())


# Convert target tanlovlari
@dp.message(F.text.in_([BTN_TARGET_PDF, BTN_TARGET_PNG, BTN_TARGET_DOCX, BTN_TARGET_PPTX]))
async def set_target(m: Message):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "convert":
        return await m.answer("Maqsad format bu sessiyada emas.", reply_markup=kb_main())
    mapping = {
        BTN_TARGET_PDF: "pdf",
        BTN_TARGET_PNG: "png",
        BTN_TARGET_DOCX: "docx",
        BTN_TARGET_PPTX: "pptx",
    }
    s["params"]["target"] = mapping[m.text]
    await m.answer(f"✅ Target: {mapping[m.text].upper()}", reply_markup=kb_convert())


# Photo’ni bloklash
@dp.message(F.photo)
async def reject_photo(m: Message):
    await m.reply("🖼 Rasmni **Document (File)** sifatida yuboring. (Telegram orqali fayl limiti: 10 MB)")


# Fayl qabul qilish (LIMIT bilan)
@dp.message(F.document)
async def collect_file(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return

    size_bytes = m.document.file_size or 0
    if size_bytes > MAX_FILE_SIZE:
        clear_session(m.from_user.id)
        mb = size_bytes / (1024 * 1024)
        return await m.reply(
            f"❌ Fayl juda katta: {mb:.1f} MB. Maksimum {MAX_FILE_MB} MB.\n"
            f"Jarayon bekor qilindi. Kichikroq fayl bilan qayta boshlang.",
            reply_markup=kb_main()
        )

    name = m.document.file_name or "file.bin"
    mime = m.document.mime_type or "application/octet-stream"

    data = None
    try:
        tg_file = await bot.get_file(m.document.file_id)
        buf = io.BytesIO()
        await bot.download(tg_file, destination=buf)
        data = buf.getvalue()
    except Exception as e1:
        try:
            fobj = await bot.download(m.document)
            data = fobj.read()
        except Exception as e2:
            try:
                tg_file = await bot.get_file(m.document.file_id)
                buf = io.BytesIO()
                await bot.download(tg_file, destination=buf)
                data = buf.getvalue()
            except Exception as e3:
                print("DOCUMENT DOWNLOAD ERROR:", repr(e1), repr(e2), repr(e3), file=sys.stderr)
                return await m.reply("❌ Faylni yuklab olishda xatolik. Qayta yuboring.")

    if data is None:
        return await m.reply("❌ Faylni qabul qilib bo‘lmadi.")

    op = s["op"]

    if op == "merge":
        if mime != "application/pdf":
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi.", reply_markup=kb_merge())
        s["files"].append({"name": name, "bytes": data, "mime": mime})
        return await m.reply(f"Qo‘shildi ✅  ({name})  — jami: {len(s['files'])}", reply_markup=kb_merge())

    if op in {"split", "pagenum", "watermark", "ocr", "translate"}:
        s["files"] = [{"name": name, "bytes": data, "mime": mime}]
        if mime != "application/pdf":
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi.",
                                 reply_markup=kb_pagenum() if op=="pagenum" else
                                             (kb_watermark() if op=="watermark" else
                                              (kb_ocr() if op=="ocr" else kb_translate())))
        await m.reply(
            f"Fayl qabul qilindi: {name} ({human_size(len(data))}) ✅\n"
            "Parametr(lar)ni tanlang, so‘ng ✅ Yakunlash.",
            reply_markup=kb_pagenum() if op=="pagenum" else
                        (kb_watermark() if op=="watermark" else
                         (kb_ocr() if op=="ocr" else kb_translate()))
        )
        return

    if op == "convert":
        ext = (os.path.splitext(name)[1] or "").lower()
        is_pdf_or_img = (mime == "application/pdf" or ext == ".pdf" or
                         mime in {"image/jpeg","image/png","image/webp"} or
                         ext in {".jpg",".jpeg",".png",".webp"})
        is_office = ext in {".docx",".pptx",".xlsx"}

        if is_office:
            if s["files"]:
                return await m.reply("DOCX/PPTX/XLSX bitta fayl bo‘lishi kerak. ❌ Cancel qilib qayta boshlang.",
                                     reply_markup=kb_convert())
            s["files"] = [{"name": name, "bytes": data, "mime": mime}]
            return await m.reply(f"Fayl qabul qilindi: {name} ({human_size(len(data))}) ✅\n"
                                 f"{BTN_TARGET_PDF} tanlab, so‘ng {BTN_DONE}.",
                                 reply_markup=kb_convert())

        if is_pdf_or_img:
            s["files"].append({"name": name, "bytes": data, "mime": mime})
            return await m.reply(
                f"Qo‘shildi ✅  ({name})  — jami: {len(s['files'])}\n"
                f"{BTN_TARGET_PDF} tanlab, so‘ng {BTN_DONE}.",
                reply_markup=kb_convert()
            )

        return await m.reply("Qo‘llanadigan turlar: PDF, JPG, PNG, WEBP yoki bitta DOCX/PPTX/XLSX.",
                             reply_markup=kb_convert())


@dp.message(F.text == BTN_DONE)
async def done_handler(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Sessiya yo‘q.", reply_markup=kb_main())

    op = s["op"]; files = s["files"]; p = s["params"]

    try:
        if op == "split":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_split())
            if "range" not in p: return await m.answer("🧭 Set Range tanlang.", reply_markup=kb_split())
            out = pdf_split_bytes(files[0]["bytes"], p["range"])
            if not out: return await m.answer("Ajratishda xatolik.", reply_markup=kb_split())
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="split.pdf"),
                                    caption="✅ Split tayyor")
            clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "merge":
            if len(files) < 2: return await m.answer("Kamida 2 ta PDF yuboring.", reply_markup=kb_merge())
            out = pdf_merge_bytes([f["bytes"] for f in files])
            if not out: return await m.answer("Merge xatolik.", reply_markup=kb_merge())
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="merge.pdf"),
                                    caption="✅ Merge tayyor")
            clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "pagenum":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_pagenum())
            pos = p.get("pos", "bottom-right")
            out = pdf_overlay_text(files[0]["bytes"], text="{page}", pos=pos, font_size=10)
            if not out: return await m.answer("Sahifa raqami qo‘shishda xatolik.", reply_markup=kb_pagenum())
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="pagenum.pdf"),
                                    caption="✅ Sahifa raqamlari qo‘shildi")
            clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "watermark":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_watermark())
            wm = p.get("wm")
            if not wm: return await m.answer("📝 Set Text tanlang.", reply_markup=kb_watermark())
            pos = p.get("pos", "bottom-right")
            out = pdf_overlay_text(files[0]["bytes"], text=wm, pos=pos, font_size=14)
            if not out: return await m.answer("Watermarkda xatolik.", reply_markup=kb_watermark())
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="watermark.pdf"),
                                    caption="✅ Watermark qo‘shildi")
            clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "ocr":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_ocr())
            lang = p.get("lang", "eng")
            txt = ocr_pdf_to_text(files[0]["bytes"], lang=lang)
            if not txt: return await m.answer("OCR natijasi bo‘sh. Tilni tekshiring.", reply_markup=kb_ocr())
            await bot.send_document(m.chat.id, BufferedInputFile(txt.encode("utf-8"), filename="ocr.txt"),
                                    caption=f"✅ OCR tayyor (lang={lang})")
            clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "translate":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_translate())
            to = p.get("to", "uz")
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(files[0]["bytes"]))
                src_text = "\n\n".join([pg.extract_text() or "" for pg in reader.pages]).strip()
            except Exception:
                src_text = ""
            if not src_text:
                return await m.answer("PDFdan matn olinmadi. Avval OCR qilib ko‘ring.", reply_markup=kb_translate())
            out_text = src_text
            try:
                from googletrans import Translator
                tr = Translator()
                out_text = tr.translate(src_text, dest=to).text
            except Exception as e:
                print("TRANSLATE WARN:", repr(e), file=sys.stderr)
            await bot.send_document(m.chat.id, BufferedInputFile(out_text.encode("utf-8"),
                                                                 filename=f"translate_{to}.txt"),
                                    caption=f"✅ Tarjima tayyor (->{to})")
            clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "convert":
            if not files: return await m.answer("Fayl yuboring.", reply_markup=kb_convert())
            target = p.get("target", "pdf")
            name = files[0]["name"].lower()
            data = files[0]["bytes"]

            # A) Target PDF va ko‘p rasm/PDF → bitta PDF
            if target == "pdf" and (len(files) > 1 or any(
                (f["mime"] != "application/pdf" and os.path.splitext(f["name"].lower())[1] in {".jpg",".jpeg",".png",".webp"})
                for f in files
            )):
                pdf_parts = []
                for f in files:
                    b = ensure_pdf_bytes(f["name"], f["bytes"], f["mime"])
                    if not b:
                        return await m.answer(f"{f['name']} → PDFga o‘tkaza olmadim (faqat JPG/PNG/WEBP/PDF).",
                                              reply_markup=kb_convert())
                    pdf_parts.append(b)
                out = pdf_parts[0] if len(pdf_parts) == 1 else pdf_merge_bytes(pdf_parts)
                if not out:
                    return await m.answer("PDF yig‘ishda xatolik.", reply_markup=kb_convert())
                await bot.send_document(m.chat.id, BufferedInputFile(out, filename="converted.pdf"),
                                        caption="✅ Birlashtirilgan PDF tayyor")
                clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

            # B) Office → PDF (yagona fayl)
            if target == "pdf" and name.endswith((".docx",".pptx",".xlsx")):
                out = libre_convert(data, "pdf", in_name=name)
                if not out:
                    return await m.answer("Konvert xatolik.", reply_markup=kb_convert())
                await bot.send_document(
                    m.chat.id,
                    BufferedInputFile(out, filename=f"{os.path.splitext(name)[0]}.pdf"),
                    caption="✅ PDF tayyor"
                )
                clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

            # C) PNG (1-sahifa/slayd)
            if target == "png" and (name.endswith(".pptx") or name.endswith(".pdf")):
                try:
                    from pdf2image import convert_from_bytes
                    if name.endswith(".pptx"):
                        pdf = libre_convert(data, "pdf", in_name=name)
                        if not pdf: return await m.answer("PPTX->PDF xatolik.", reply_markup=kb_convert())
                        pages = convert_from_bytes(pdf, dpi=180, first_page=1, last_page=1)
                    else:
                        pages = convert_from_bytes(data, dpi=180, first_page=1, last_page=1)
                    buf = io.BytesIO()
                    pages[0].save(buf, format="PNG")
                    await bot.send_document(
                        m.chat.id,
                        BufferedInputFile(buf.getvalue(), filename=f"{os.path.splitext(name)[0]}_1.png"),
                        caption="✅ PNG (1-sahifa/slayd)"
                    )
                    clear_session(m.from_user.id); return await m.answer("✅ Tugadi.", reply_markup=kb_main())
                except Exception as e:
                    print("PNG CONVERT ERROR:", repr(e), file=sys.stderr)
                    return await m.answer("PNG konvert xatolik (poppler/tesseract o‘rnatilganini tekshiring).",
                                          reply_markup=kb_convert())

            return await m.answer("Bu yo‘nalish hozircha qo‘llanmaydi.", reply_markup=kb_convert())

    except Exception as e:
        print("DONE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        await m.answer("❌ Jarayon davomida xatolik.", reply_markup=kb_main())


# =========================
# Commands list (still useful)
# =========================
async def _set_commands():
    cmds = [
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="new_resume", description="Web rezyume forma"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="pdf_split", description="PDF ajratish"),
        BotCommand(command="pdf_merge", description="PDF qo‘shish"),
        BotCommand(command="pagenum", description="Sahifa raqami"),
        BotCommand(command="watermark", description="Watermark"),
        BotCommand(command="ocr", description="OCR"),
        BotCommand(command="convert", description="Konvertatsiya"),
        BotCommand(command="translate", description="Tarjima"),
        BotCommand(command="status", description="Holat"),
        BotCommand(command="done", description="Yakunlash"),
        BotCommand(command="cancel", description="Bekor"),
    ]
    try:
        await bot.set_my_commands(cmds)
        print("✅ Bot commands list yangilandi")
    except Exception as e:
        print("SET COMMANDS ERROR:", repr(e), file=sys.stderr)


@app.on_event("startup")
async def on_startup():
    await _set_commands()


# =========================
# Webhook
# =========================
@app.post("/bot/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    try:
        if hasattr(dp, "feed_raw_update"):
            await dp.feed_raw_update(bot, data)
        else:
            upd = Update.model_validate(data)
            await dp.feed_update(bot, upd)
        return {"ok": True}
    except Exception as e:
        print("=== WEBHOOK ERROR ===", repr(e), file=sys.stderr)
        traceback.print_exc()
        print("Update JSON:", data, file=sys.stderr)
        return {"ok": False}


@app.get("/bot/set_webhook")
async def set_webhook(base: str | None = None):
    base_url = (base or APP_BASE).rstrip("/")
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}


# =========================
# Debug
# =========================
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}


@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}
