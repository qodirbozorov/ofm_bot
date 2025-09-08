# app/main.py
import os
import io
import re
import sys
import json
import glob
import time
import shutil
import random
import string
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
    Message, Update,
    FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext


# =========================
# CONFIG (env kerak emas)
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE  = "https://ofmbot-production.up.railway.app"
GROUP_CHAT_ID = -1003046464831

# Tesseract lang-auto uchun default kandidatlar (mavjud bo'lganlarini ishlatadi)
TESS_CANDIDATES = os.getenv("TESS_LANGS", "eng+uzb+rus")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# =========================
# GLOBAL STATE (yengil)
# =========================
ACTIVE_USERS: set[int] = set()
SESS: Dict[int, Dict[str, Any]] = {}   # {"op":str, "files":[{path,name,mime}], "params":{}}
PENDING: Dict[int, List[Dict[str, str]]] = {}  # sessiyasiz kelgan fayllar (tavsiyalar uchun)

STATS = {
    "users": set(),
    "resume": 0,
    "split": 0,
    "merge": 0,
    "pagenum": 0,
    "watermark": 0,
    "ocr": 0,
    "convert": 0,
    "translate": 0,
    "received_photos": 0,
    "received_docs": 0,
    "received_pdf": 0,
    "received_images": 0,
    "received_office": 0,
    "received_others": 0,
}

# Ish vaqtida vaqtinchalik fayllar uchun ildiz
TMP_ROOT = "/tmp/ofm_bot"


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def user_tmp_dir(uid: int) -> str:
    d = os.path.join(TMP_ROOT, str(uid))
    ensure_dir(d)
    return d


def rnd_tag(n=6):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


def clean_user_tmp(uid: int):
    d = user_tmp_dir(uid)
    try:
        for f in glob.glob(os.path.join(d, "*")):
            try: os.remove(f)
            except: pass
    except: pass


def get_session(uid: int) -> Optional[Dict[str, Any]]:
    return SESS.get(uid)


def new_session(uid: int, op: str):
    clean_user_tmp(uid)
    SESS[uid] = {"op": op, "files": [], "params": {}}


def clear_session(uid: int):
    SESS.pop(uid, None)
    PENDING.pop(uid, None)
    clean_user_tmp(uid)


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
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"])
)

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

# --- Minimal Bootstrap admin panel
ADMIN_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<title>OFM Bot – Admin</title>
</head><body class="bg-light">
<div class="container py-4">
  <h1 class="mb-4">📊 OFM Bot Dashboard</h1>
  <div class="row g-3">
    <div class="col-md-3"><div class="card"><div class="card-body">
      <h6 class="text-muted">Unikal foydalanuvchilar</h6>
      <div class="display-6">{{ users_count }}</div>
    </div></div></div>
    {% for k,v in counters.items() %}
    <div class="col-md-3"><div class="card"><div class="card-body">
      <h6 class="text-muted">{{ k }}</h6>
      <div class="display-6">{{ v }}</div>
    </div></div></div>
    {% endfor %}
  </div>
  <hr class="my-4" />
  <p class="text-muted">Yengil statistik ko‘rsatkichlar (RAM) – restartda tozalanadi.</p>
</div>
</body></html>
"""

@app.get("/admin", response_class=HTMLResponse)
def admin():
    counters = {
        "Rezyume": STATS["resume"],
        "Split": STATS["split"],
        "Merge": STATS["merge"],
        "PageNum": STATS["pagenum"],
        "Watermark": STATS["watermark"],
        "OCR": STATS["ocr"],
        "Convert": STATS["convert"],
        "Translate": STATS["translate"],
        "Photo qabul": STATS["received_photos"],
        "Doc qabul": STATS["received_docs"],
        "PDF qabul": STATS["received_pdf"],
        "Image doc qabul": STATS["received_images"],
        "Office doc qabul": STATS["received_office"],
        "Boshqa": STATS["received_others"],
    }
    t = Environment().from_string(ADMIN_HTML)
    return t.render(users_count=len(STATS["users"]), counters=counters)


# =========================
# Resume helpers (unchanged logic)
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
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, in_path], check=True)
            with open(out_path, "rb") as f:
                return f.read()
        except Exception as e:
            print("DOCX->PDF ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None

def libre_convert_path(in_path: str, out_ext: str) -> Optional[str]:
    td = os.path.dirname(in_path)
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", out_ext, "--outdir", td, in_path], check=True)
        for fn in os.listdir(td):
            if fn != os.path.basename(in_path) and fn.lower().endswith(f".{out_ext}"):
                return os.path.join(td, fn)
        return None
    except Exception as e:
        print("LIBRE CONVERT ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


# =========================
# PDF helpers (lazy imports)
# =========================
def pdf_split_bytes(pdf_path: str, range_str: str) -> Optional[str]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
        with open(pdf_path, "rb") as rf:
            reader = PdfReader(rf)
            writer = PdfWriter()
            total = len(reader.pages)
            wanted: List[int] = []
            for chunk in re.split(r"[,\s]+", (range_str or "").strip()):
                if not chunk: continue
                if "-" in chunk:
                    a, b = chunk.split("-", 1)
                    a = max(1, int(a)); b = min(total, int(b))
                    if a <= b: wanted.extend(range(a, b+1))
                else:
                    p = int(chunk)
                    if 1 <= p <= total: wanted.append(p)
            if not wanted: return None
            for p in wanted:
                writer.add_page(reader.pages[p-1])
            out_path = pdf_path + f".split.{rnd_tag()}.pdf"
            with open(out_path, "wb") as wf:
                writer.write(wf)
            return out_path
    except Exception as e:
        print("PDF SPLIT ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None

def pdf_merge_paths(paths: List[str]) -> Optional[str]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
        writer = PdfWriter()
        for pth in paths:
            with open(pth, "rb") as rf:
                r = PdfReader(rf)
                for pg in r.pages:
                    writer.add_page(pg)
        out_path = paths[0] + f".merge.{rnd_tag()}.pdf"
        with open(out_path, "wb") as wf:
            writer.write(wf)
        return out_path
    except Exception as e:
        print("PDF MERGE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None

def pdf_overlay_text(pdf_path: str, text: str, pos: str = "bottom-right", font_size: int = 10) -> Optional[str]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
        out_path = pdf_path + f".ovl.{rnd_tag()}.pdf"
        with open(pdf_path, "rb") as rf:
            reader = PdfReader(rf)
            writer = PdfWriter()
            for i, page in enumerate(reader.pages, start=1):
                media = page.mediabox
                w, h = float(media.width), float(media.height)
                packet = io.BytesIO()
                c = canvas.Canvas(packet, pagesize=(w, h))
                c.setFont("Helvetica", font_size)

                txt = text.replace("{page}", str(i))
                margin = 20
                tw = c.stringWidth(txt, "Helvetica", font_size)
                th = font_size + 2
                x, y = margin, margin
                if "top" in pos: y = h - th - margin
                if "bottom" in pos: y = margin
                if "right" in pos: x = w - tw - margin
                if "left" in pos: x = margin
                if "center" in pos: x = (w - tw) / 2
                c.drawString(x, y, txt)
                c.save()

                packet.seek(0)
                overlay = PdfReader(packet).pages[0]
                page.merge_page(overlay)
                writer.add_page(page)
            with open(out_path, "wb") as wf:
                writer.write(wf)
        return out_path
    except Exception as e:
        print("PDF OVERLAY ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


# =========================
# OCR (auto-lang, image & pdf, no preview)
# =========================
def _ocr_image_pil(img) -> str:
    try:
        import pytesseract
        # Orientation + script detection (agar mavjud bo'lsa)
        try:
            osd = pytesseract.image_to_osd(img)
            angle_m = re.search(r"Rotate: (\d+)", osd or "")
            if angle_m:
                angle = int(angle_m.group(1))
                if angle != 0:
                    img = img.rotate(-angle, expand=True)
        except Exception:
            pass

        txt = pytesseract.image_to_string(img, lang=TESS_CANDIDATES)
        return txt.strip()
    except Exception as e:
        print("OCR IMG ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return ""

def ocr_any_to_text(path: str, mime: str) -> str:
    try:
        from PIL import Image
        if mime.startswith("image/"):
            img = Image.open(path).convert("RGB")
            return _ocr_image_pil(img)
        elif mime == "application/pdf":
            from pdf2image import convert_from_path
            pages = convert_from_path(path, dpi=200)
            chunks = []
            for pg in pages:
                chunks.append(_ocr_image_pil(pg))
            return "\n\n".join(chunks).strip()
        else:
            return ""
    except Exception as e:
        print("OCR ANY ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return ""


# =========================
# WebApp: Resume (bo'sh bo'lsa ham xato bermaydi)
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

    photo: UploadFile | None = None,
):
    def nz(v, default=""): return v if v is not None else default

    full_name = nz(full_name); phone = nz(phone); tg_id_str = nz(tg_id)
    birth_date = nz(birth_date); birth_place = nz(birth_place)
    nationality = nz(nationality, "O‘zbek"); party_membership = nz(party_membership, "Yo‘q")
    education = nz(education); university = nz(university); specialization = nz(specialization, "Yo‘q")
    ilmiy_daraja = nz(ilmiy_daraja, "Yo‘q"); ilmiy_unvon = nz(ilmiy_unvon, "Yo‘q")
    languages = nz(languages, "Yo‘q"); dav_mukofoti = nz(dav_mukofoti, "Yo‘q")
    deputat = nz(deputat, "Yo‘q"); adresss = nz(adresss)
    current_position_date = nz(current_position_date); current_position_full = nz(current_position_full)
    work_experience = nz(work_experience)

    try:
        rels = json.loads(relatives) if relatives else []
        if not isinstance(rels, list): rels = []
    except Exception:
        rels = []

    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx template topilmadi"}, status_code=200)

    ctx = {
        "full_name": full_name, "phone": phone,
        "birth_date": birth_date, "birth_place": birth_place,
        "nationality": nationality, "party_membership": party_membership,
        "education": education, "university": university, "specialization": specialization,
        "ilmiy_daraja": ilmiy_daraja, "ilmiy_unvon": ilmiy_unvon, "languages": languages,
        "dav_mukofoti": dav_mukofoti, "deputat": deputat, "adresss": adresss,
        "current_position_date": current_position_date, "current_position_full": current_position_full,
        "work_experience": work_experience, "relatives": rels,
    }

    # DOCX render
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
    pdf_name  = f"{base_name}_0.pdf"
    img_name  = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # Guruhga: rasm + JSON
    try:
        if img_bytes:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img_bytes, filename=img_name),
                caption=f"🆕 Forma: {full_name or '—'}\n📞 {phone or '—'}\n👤 TG: {tg_id_str or '—'}"
            )
        payload = {
            "timestamp": datetime.utcnow().isoformat()+"Z",
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
        await bot.send_document(GROUP_CHAT_ID, BufferedInputFile(jb, filename=json_name),
                                caption=f"📄 JSON: {full_name or '—'}")
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)

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

    STATS["resume"] += 1
    STATS["users"].add(chat_id or 0)
    return {"status": "success", "close": True}


# =========================
# Reply Keyboards
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

# Suggestion buttons (kontekstga qarab)
SUG_IMG_TO_PDF = "🖼→📄 Rasmni PDFga"
SUG_IMG_OCR    = "🖼🪄 OCR"
SUG_PDF_SPLIT  = "PDF ✂️ Split"
SUG_PDF_PNUM   = "PDF 🔢 PageNum"
SUG_PDF_WM     = "PDF 💧 Watermark"
SUG_PDF_OCR    = "PDF 🪄 OCR"
SUG_PDF_TR     = "PDF 🌐 Translate"
SUG_OFFICE2PDF = "Office → PDF"

# Positions
BTN_TL = "↖️"; BTN_TC = "⬆️"; BTN_TR = "↗️"
BTN_BL = "↙️"; BTN_BC = "⬇️"; BTN_BR = "↘️"

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

def kb_suggest(button_rows: List[List[str]]) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=t) for t in row] for row in button_rows]
    rows.append([KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def kb_split() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧭 Oraliq kiriting (matn)"), KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )

def kb_merge() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Yana PDF yuborish"), KeyboardButton(text=BTN_DONE)],
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
            [KeyboardButton(text="📝 Watermark matni (matn)"),],
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
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )

def kb_convert() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Target: PDF"), KeyboardButton(text="🎯 Target: PNG")],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )

def kb_translate() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 Maqsad til (uz/ru/en …) yozing")],
            [KeyboardButton(text=BTN_DONE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True
    )

POS_MAP = {BTN_TL:"top-left", BTN_TC:"top-center", BTN_TR:"top-right",
           BTN_BL:"bottom-left", BTN_BC:"bottom-center", BTN_BR:"bottom-right"}


# =========================
# FSM states (free text inputs)
# =========================
class SplitRangeSG(StatesGroup):
    waiting = State()
class WMTextSG(StatesGroup):
    waiting = State()
class TargetLangSG(StatesGroup):
    waiting = State()


# =========================
# Bot flows (commands + reply UX)
# =========================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    STATS["users"].add(m.from_user.id)
    await m.answer(
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n"
        f"Assalom! Pastdagi menyudan funksiya tanlang yoki fayl yuboring.",
        reply_markup=kb_main()
    )

@dp.message(F.text == BTN_HELP)
@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "📌 Qisqa qo‘llanma:\n"
        "• 🧾 Rezyume forma – chatdagi tugma orqali web formani ochadi.\n"
        "• Fayl yuborsangiz, bot mos variantlarni taklif qiladi (Split/Merge/OCR/Convert…).\n"
        "• Parametr kerak bo‘lsa (range, watermark matni) – bot so‘raydi.\n"
        "• ✅ Yakunlash – natija fayl sifatida keladi.\n"
        "• ↩️ Back – asosiy menyuga, ❌ Cancel – jarayonni tozalash.",
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

# ---- Split
@dp.message(F.text == BTN_SPLIT)
async def flow_split(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "split")
    await m.answer(
        "✂️ PDF Split.\n1) PDF yuboring\n2) 🧭 Oraliq kiriting (masalan: 1-3,7)\n3) ✅ Yakunlash",
        reply_markup=kb_split()
    )

@dp.message(F.text.startswith("🧭 "))
async def ask_split_range(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "split":
        return await m.answer("Bu parametr Split sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(SplitRangeSG.waiting)
    await m.answer("Oraliq kiriting (masalan: 1-3,7):")

@dp.message(SplitRangeSG.waiting, F.text)
async def got_range(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    s["params"]["range"] = (m.text or "").strip()
    await state.clear()
    await m.answer("✅ Oraliq qabul qilindi.", reply_markup=kb_split())

# ---- Merge
@dp.message(F.text == BTN_MERGE)
async def flow_merge(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "merge")
    await m.answer("🧷 PDF Merge.\nKetma-ket PDF yuboring, so‘ng ✅ Yakunlash.", reply_markup=kb_merge())

# ---- Page numbers
@dp.message(F.text == BTN_PAGENUM)
async def flow_pagenum(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "pagenum")
    await m.answer("🔢 Sahifa raqami.\n1) PDF yuboring\n2) Joylashuvni tanlang\n3) ✅ Yakunlash", reply_markup=kb_pagenum())

# ---- Watermark
@dp.message(F.text == BTN_WM)
async def flow_wm(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "watermark")
    await m.answer("💧 Watermark.\n1) PDF yuboring\n2) 📝 Watermark matni\n3) Joylashuv\n4) ✅ Yakunlash", reply_markup=kb_watermark())

@dp.message(F.text.startswith("📝 "))
async def ask_wm_text(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "watermark":
        return await m.answer("Bu parametr Watermark sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(WMTextSG.waiting)
    await m.answer("Watermark matnini kiriting:")

@dp.message(WMTextSG.waiting, F.text)
async def got_wm_text(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    txt = (m.text or "").strip()
    if not txt:
        return await m.answer("Matn bo‘sh bo‘lmasin.")
    s["params"]["wm"] = txt[:150]
    await state.clear()
    await m.answer("✅ Watermark matni qabul qilindi.", reply_markup=kb_watermark())

# Pozitsiya tugmalari (pagenum & watermark)
@dp.message(F.text.in_([BTN_TL, BTN_TC, BTN_TR, BTN_BL, BTN_BC, BTN_BR]))
async def set_position(m: Message):
    s = get_session(m.from_user.id)
    if not s or s["op"] not in {"pagenum", "watermark"}:
        return await m.answer("Joylashuv tanlash bu sessiyada emas.", reply_markup=kb_main())
    s["params"]["pos"] = POS_MAP[m.text]
    await m.answer(f"✅ Pozitsiya: {POS_MAP[m.text]}",
                   reply_markup=kb_pagenum() if s["op"]=="pagenum" else kb_watermark())

# ---- OCR (auto-lang)
@dp.message(F.text == BTN_OCR)
async def flow_ocr(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "ocr")
    await m.answer("🪄 OCR.\nPDF yoki rasm yuboring – til avtomatik aniqlanadi.\nSo‘ng ✅ Yakunlash.", reply_markup=kb_ocr())

# ---- Convert
@dp.message(F.text == BTN_CONVERT)
async def flow_convert(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "convert")
    await m.answer(
        "🔁 Convert.\n"
        "• Ko‘p JPG/PNG/PDF yuborsangiz → 🎯 Target: PDF → ✅ Yakunlash (hammasi bitta PDF).\n"
        "• DOCX/PPTX/XLSX → 🎯 Target: PDF → ✅ Yakunlash.\n"
        "• PPTX/PDF → 🎯 Target: PNG → ✅ Yakunlash (1-sahifa/slayd).",
        reply_markup=kb_convert()
    )

@dp.message(F.text.startswith("🎯 Target:"))
async def set_target(m: Message):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "convert":
        return await m.answer("Maqsad format bu sessiyada emas.", reply_markup=kb_main())
    val = (m.text or "").lower()
    if "pdf" in val:  s["params"]["target"] = "pdf"
    elif "png" in val: s["params"]["target"] = "png"
    else: s["params"]["target"] = "pdf"
    await m.answer(f"✅ Target: {s['params']['target'].upper()}", reply_markup=kb_convert())

# ---- Translate
@dp.message(F.text == BTN_TRANSLATE)
async def flow_translate(m: Message, state: FSMContext):
    await state.clear()
    new_session(m.from_user.id, "translate")
    await m.answer("🌐 Translate.\nPDF yuboring → so‘ng maqsad til kodini yozing (uz/ru/en …) → ✅ Yakunlash.",
                   reply_markup=kb_translate())

@dp.message(F.text.startswith("🌐 Maqsad til"))
async def ask_target_lang(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "translate":
        return await m.answer("Bu parametr Translate sessiyasida ishlaydi.", reply_markup=kb_main())
    await state.set_state(TargetLangSG.waiting)
    await m.answer("Maqsad til kodini kiriting (uz/ru/en …):")

@dp.message(TargetLangSG.waiting, F.text)
async def got_to_lang(m: Message, state: FSMContext):
    s = get_session(m.from_user.id)
    if not s:
        await state.clear()
        return await m.answer("Sessiya topilmadi.", reply_markup=kb_main())
    s["params"]["to"] = (m.text or "").strip()
    await state.clear()
    await m.answer("✅ Maqsad til qabul qilindi.", reply_markup=kb_translate())


# =========================
# Fayl qabul qilish (RAMga emas, diskka)
# =========================
async def _download_document_to_path(document, out_path: str) -> bool:
    try:
        tg_file = await bot.get_file(document.file_id)
        with open(out_path, "wb") as f:
            await bot.download(tg_file, destination=f)
        return True
    except Exception as e:
        print("DOCUMENT DOWNLOAD ERROR:", repr(e), file=sys.stderr)
        return False

async def _download_photo_to_path(photo_sizes, out_path: str) -> bool:
    try:
        biggest = max(photo_sizes, key=lambda p: (p.width or 0) * (p.height or 0))
        tg_file = await bot.get_file(biggest.file_id)
        with open(out_path, "wb") as f:
            await bot.download(tg_file, destination=f)
        return True
    except Exception as e:
        print("PHOTO DOWNLOAD ERROR:", repr(e), file=sys.stderr)
        return False

def _classify_mime(filename: str, mime_hint: Optional[str]) -> str:
    ext = (os.path.splitext(filename or "")[1] or "").lower()
    if ext in {".pdf"}: return "application/pdf"
    if ext in {".jpg", ".jpeg"}: return "image/jpeg"
    if ext in {".png"}: return "image/png"
    if ext in {".webp"}: return "image/webp"
    if ext in {".docx"}: return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext in {".pptx"}: return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if ext in {".xlsx"}: return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return mime_hint or "application/octet-stream"

def _suggest_keyboard_for(mime: str, is_photo: bool) -> ReplyKeyboardMarkup:
    if mime.startswith("image/") or is_photo:
        # rasm
        return kb_suggest([[SUG_IMG_TO_PDF, SUG_IMG_OCR]])
    if mime == "application/pdf":
        return kb_suggest([[SUG_PDF_SPLIT, SUG_PDF_PNUM],
                           [SUG_PDF_WM, SUG_PDF_OCR],
                           [SUG_PDF_TR]])
    if mime.startswith("application/vnd.openxmlformats-officedocument"):
        return kb_suggest([[SUG_OFFICE2PDF]])
    # boshqa
    return kb_suggest([[BTN_CONVERT, BTN_OCR]])

@dp.message(F.photo)
async def handle_photo(m: Message):
    STATS["received_photos"] += 1
    uid = m.from_user.id
    d = user_tmp_dir(uid)
    name = f"photo_{int(time.time())}.jpg"
    path = os.path.join(d, name)
    ok = await _download_photo_to_path(m.photo, path)
    if not ok:
        return await m.reply("❌ Rasmni qabul qilib bo‘lmadi.", reply_markup=kb_main())

    # tavsiyalar
    PENDING.setdefault(uid, []).append({"path": path, "name": name, "mime": "image/jpeg"})
    await m.reply(
        "🖼 Rasm qabul qilindi.\nQuyidagilardan birini tanlang:",
        reply_markup=_suggest_keyboard_for("image/jpeg", is_photo=True)
    )

@dp.message(F.document)
async def handle_document(m: Message):
    STATS["received_docs"] += 1
    uid = m.from_user.id
    d = user_tmp_dir(uid)
    name = m.document.file_name or f"file_{int(time.time())}"
    path = os.path.join(d, name)
    ok = await _download_document_to_path(m.document, path)
    if not ok:
        return await m.reply("❌ Faylni qabul qilib bo‘lmadi.", reply_markup=kb_main())

    mime = _classify_mime(name, m.document.mime_type)
    if mime == "application/pdf": STATS["received_pdf"] += 1
    elif mime.startswith("image/"): STATS["received_images"] += 1
    elif mime.startswith("application/vnd.openxmlformats-officedocument"): STATS["received_office"] += 1
    else: STATS["received_others"] += 1

    PENDING.setdefault(uid, []).append({"path": path, "name": name, "mime": mime})
    await m.reply(
        f"📎 Fayl qabul qilindi: {name} ({human_size(os.path.getsize(path))})\n"
        f"Quyidagilardan birini tanlang:",
        reply_markup=_suggest_keyboard_for(mime, is_photo=False)
    )

# Notanish: video/voice/text – tushuntirish
@dp.message(F.video | F.voice | F.audio | F.sticker | F.animation)
async def explain_unsupported(m: Message):
    await m.reply(
        "ℹ️ Videolar, ovozli xabarlar, sticker/animatsiyalar bilan ishlamayman.\n"
        "PDF/rasm/dokument yuborsangiz, mos funksiyalarni taklif qilaman.",
        reply_markup=kb_main()
    )

# Suggestion bosilganda: pending fayl(lar)ni sessiyaga ko‘chirib, oqimni boshlash
@dp.message(F.text.in_([
    SUG_IMG_TO_PDF, SUG_IMG_OCR, SUG_PDF_SPLIT, SUG_PDF_PNUM,
    SUG_PDF_WM, SUG_PDF_OCR, SUG_PDF_TR, SUG_OFFICE2PDF
]))
async def suggestion_selected(m: Message):
    uid = m.from_user.id
    pend = PENDING.get(uid) or []
    if not pend:
        return await m.reply("Fayl yuboring, so‘ng tanlang.", reply_markup=kb_main())

    # avtomatik oqim
    txt = m.text
    if txt == SUG_IMG_TO_PDF:
        new_session(uid, "convert")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        SESS[uid]["params"]["target"] = "pdf"
        await m.reply("🔁 Convert: rasm(lar)ni PDFga aylantirish.\n✅ Yakunlash bosing.", reply_markup=kb_convert())
    elif txt == SUG_IMG_OCR:
        new_session(uid, "ocr")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        await m.reply("🪄 OCR: rasm(lar)dan matn chiqarish.\n✅ Yakunlash bosing.", reply_markup=kb_ocr())
    elif txt == SUG_PDF_SPLIT:
        new_session(uid, "split")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        await m.reply("✂️ Split: PDF oraliq kiriting, so‘ng ✅ Yakunlash.", reply_markup=kb_split())
    elif txt == SUG_PDF_PNUM:
        new_session(uid, "pagenum")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        await m.reply("🔢 PageNum: joylashuvni tanlang, so‘ng ✅ Yakunlash.", reply_markup=kb_pagenum())
    elif txt == SUG_PDF_WM:
        new_session(uid, "watermark")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        await m.reply("💧 Watermark: matn va joylashuvni bering, so‘ng ✅ Yakunlash.", reply_markup=kb_watermark())
    elif txt == SUG_PDF_OCR:
        new_session(uid, "ocr")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        await m.reply("🪄 OCR: PDFdan matn chiqarish.\n✅ Yakunlash bosing.", reply_markup=kb_ocr())
    elif txt == SUG_PDF_TR:
        new_session(uid, "translate")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        await m.reply("🌐 Translate: maqsad tilini yozing (uz/ru/en …), so‘ng ✅ Yakunlash.", reply_markup=kb_translate())
    elif txt == SUG_OFFICE2PDF:
        new_session(uid, "convert")
        SESS[uid]["files"] = pend; PENDING[uid] = []
        SESS[uid]["params"]["target"] = "pdf"
        await m.reply("🔁 Convert: Office → PDF.\n✅ Yakunlash bosing.", reply_markup=kb_convert())


# =========================
# DONE (yakunlash)
# =========================
@dp.message(F.text == BTN_DONE)
async def done_handler(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Sessiya yo‘q.", reply_markup=kb_main())

    op   = s["op"]
    files= s["files"]
    p    = s["params"]
    uid  = m.from_user.id

    try:
        if op == "split":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_split())
            if files[0]["mime"] != "application/pdf": return await m.answer("PDF kerak.", reply_markup=kb_split())
            r = p.get("range")
            if not r: return await m.answer("Oraliq kiriting.", reply_markup=kb_split())
            out = pdf_split_bytes(files[0]["path"], r)
            if not out: return await m.answer("Ajratishda xatolik.", reply_markup=kb_split())
            await bot.send_document(m.chat.id, FSInputFile(out, filename="split.pdf"), caption="✅ Split tayyor")
            STATS["split"] += 1
            clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "merge":
            if len(files) < 2: return await m.answer("Kamida 2 ta PDF yuboring.", reply_markup=kb_merge())
            for f in files:
                if f["mime"] != "application/pdf":
                    return await m.answer("Barchasi PDF bo‘lishi kerak.", reply_markup=kb_merge())
            out = pdf_merge_paths([f["path"] for f in files])
            if not out: return await m.answer("Merge xatolik.", reply_markup=kb_merge())
            await bot.send_document(m.chat.id, FSInputFile(out, filename="merge.pdf"), caption="✅ Merge tayyor")
            STATS["merge"] += 1
            clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "pagenum":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_pagenum())
            pos = p.get("pos", "bottom-right")
            if files[0]["mime"] != "application/pdf":
                return await m.answer("PDF yuboring.", reply_markup=kb_pagenum())
            out = pdf_overlay_text(files[0]["path"], text="{page}", pos=pos, font_size=10)
            if not out: return await m.answer("Sahifa raqami qo‘shishda xatolik.", reply_markup=kb_pagenum())
            await bot.send_document(m.chat.id, FSInputFile(out, filename="pagenum.pdf"),
                                    caption="✅ Sahifa raqamlari qo‘shildi")
            STATS["pagenum"] += 1
            clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "watermark":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_watermark())
            wm = p.get("wm")
            if not wm: return await m.answer("Watermark matnini kiriting.", reply_markup=kb_watermark())
            pos = p.get("pos", "bottom-right")
            if files[0]["mime"] != "application/pdf":
                return await m.answer("PDF yuboring.", reply_markup=kb_watermark())
            out = pdf_overlay_text(files[0]["path"], text=wm, pos=pos, font_size=14)
            if not out: return await m.answer("Watermarkda xatolik.", reply_markup=kb_watermark())
            await bot.send_document(m.chat.id, FSInputFile(out, filename="watermark.pdf"),
                                    caption="✅ Watermark qo‘shildi")
            STATS["watermark"] += 1
            clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "ocr":
            if not files: return await m.answer("PDF yoki rasm yuboring.", reply_markup=kb_ocr())
            results = []
            for f in files:
                txt = ocr_any_to_text(f["path"], f["mime"])
                if txt: results.append(txt)
            out_txt = "\n\n".join(results).strip()
            if not out_txt: return await m.answer("OCR natijasi bo‘sh chiqdi.", reply_markup=kb_ocr())
            out_path = os.path.join(user_tmp_dir(uid), f"ocr_{rnd_tag()}.txt")
            with open(out_path, "w", encoding="utf-8") as wf:
                wf.write(out_txt)
            await bot.send_document(m.chat.id, FSInputFile(out_path, filename=os.path.basename(out_path)),
                                    caption=f"✅ OCR tayyor (auto-lang: {TESS_CANDIDATES})")
            STATS["ocr"] += 1
            clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "translate":
            if not files: return await m.answer("PDF yuboring.", reply_markup=kb_translate())
            if files[0]["mime"] != "application/pdf":
                return await m.answer("PDF yuboring.", reply_markup=kb_translate())
            try:
                from PyPDF2 import PdfReader
                with open(files[0]["path"], "rb") as rf:
                    reader = PdfReader(rf)
                    src_text = "\n\n".join([(pg.extract_text() or "") for pg in reader.pages]).strip()
            except Exception:
                src_text = ""
            if not src_text:
                return await m.answer("PDF ichidan matn olinmadi. Avval OCR qilib ko‘ring.", reply_markup=kb_translate())
            to = p.get("to", "uz")
            out_text = src_text
            try:
                from googletrans import Translator
                tr = Translator()
                out_text = tr.translate(src_text, dest=to).text
            except Exception as e:
                print("TRANSLATE WARN:", repr(e), file=sys.stderr)
            out_path = os.path.join(user_tmp_dir(uid), f"translate_{to}_{rnd_tag()}.txt")
            with open(out_path, "w", encoding="utf-8") as wf:
                wf.write(out_text)
            await bot.send_document(m.chat.id, FSInputFile(out_path, filename=os.path.basename(out_path)),
                                    caption=f"✅ Tarjima tayyor (->{to})")
            STATS["translate"] += 1
            clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

        if op == "convert":
            if not files: return await m.answer("Fayl yuboring.", reply_markup=kb_convert())
            target = p.get("target", "pdf")

            # A) target=pdf va ko‘p rasm/PDF → bitta PDF
            if target == "pdf":
                # Agar office bo‘lsa – bitta fayl
                if len(files) == 1 and files[0]["mime"].startswith("application/vnd.openxmlformats-"):
                    out = libre_convert_path(files[0]["path"], "pdf")
                    if not out: return await m.answer("Office → PDF konvert xatolik.", reply_markup=kb_convert())
                    await bot.send_document(m.chat.id, FSInputFile(out, filename=os.path.basename(out)),
                                            caption="✅ PDF tayyor")
                    STATS["convert"] += 1
                    clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

                # Barcha rasm/PDF’ni PDFga normalizatsiya qilib merge
                pdf_parts = []
                for f in files:
                    if f["mime"] == "application/pdf":
                        pdf_parts.append(f["path"])
                    elif f["mime"].startswith("image/"):
                        # rasm → 1 page PDF (reportlab)
                        try:
                            from PIL import Image
                            from reportlab.pdfgen import canvas
                            from reportlab.lib.utils import ImageReader
                            img = Image.open(f["path"]).convert("RGB")
                            w, h = img.size
                            out_p = f["path"] + f".imgpdf.{rnd_tag()}.pdf"
                            c = canvas.Canvas(out_p, pagesize=(w, h))
                            c.drawImage(ImageReader(img), 0, 0, width=w, height=h,
                                        preserveAspectRatio=True, anchor='sw')
                            c.showPage(); c.save()
                            pdf_parts.append(out_p)
                        except Exception as e:
                            print("IMG->PDF ERROR:", repr(e), file=sys.stderr)
                    else:
                        return await m.answer("Qo‘llab-quvvatlanmaydigan tur. Rasm/PDF yuboring.",
                                              reply_markup=kb_convert())
                if not pdf_parts:
                    return await m.answer("PDF yig‘ish uchun mos fayl yo‘q.", reply_markup=kb_convert())
                out = pdf_merge_paths(pdf_parts) if len(pdf_parts) > 1 else pdf_parts[0]
                await bot.send_document(m.chat.id, FSInputFile(out, filename=os.path.basename(out)),
                                        caption="✅ Birlashtirilgan PDF")
                STATS["convert"] += 1
                clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())

            # B) target=png (PPTX/PDF dan 1-sahifa PNG)
            if target == "png":
                f = files[0]
                path = f["path"]; mime = f["mime"]
                try:
                    from pdf2image import convert_from_path
                    if mime == "application/pdf":
                        pages = convert_from_path(path, dpi=180, first_page=1, last_page=1)
                    elif path.lower().endswith(".pptx"):
                        pdf_p = libre_convert_path(path, "pdf")
                        if not pdf_p: return await m.answer("PPTX → PDF xatolik.", reply_markup=kb_convert())
                        pages = convert_from_path(pdf_p, dpi=180, first_page=1, last_page=1)
                    else:
                        return await m.answer("PNG target hozircha PDF/PPTX uchun.", reply_markup=kb_convert())
                    out_png = path + f".{rnd_tag()}.png"
                    pages[0].save(out_png, format="PNG")
                    await bot.send_document(m.chat.id, FSInputFile(out_png, filename=os.path.basename(out_png)),
                                            caption="✅ PNG (1-sahifa)")
                    STATS["convert"] += 1
                    clear_session(uid); return await m.answer("✅ Tugadi.", reply_markup=kb_main())
                except Exception as e:
                    print("PNG CONVERT ERROR:", repr(e), file=sys.stderr)
                    return await m.answer("PNG konvert xatolik (poppler o‘rnatilganini tekshiring).",
                                          reply_markup=kb_convert())

            return await m.answer("Bu yo‘nalish hozircha qo‘llanmaydi.", reply_markup=kb_convert())

    except Exception as e:
        print("DONE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        await m.answer("❌ Jarayon davomida xatolik.", reply_markup=kb_main())


# =========================
# Commands (slash menyu ham yangilanadi)
# =========================
async def _set_commands():
    cmds = [
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="new_resume", description="Web rezyume forma"),
        BotCommand(command="help", description="Yordam"),
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
    ensure_dir(TMP_ROOT)
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
