# app/main.py
import os
import io
import re
import sys
import json
import math
import shutil
import traceback
import tempfile
import subprocess
from datetime import datetime
from typing import Optional, List, Dict
PORT = int(os.getenv("PORT", "8080"))          # Railway qo‘yadi
BOT_TOKEN = os.getenv("BOS_TOKEN", "")


from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    Message, Update,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, BotCommand
)

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from PIL import Image
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pdf2image import convert_from_path
import pytesseract
from googletrans import Translator

# =========================
# CONFIG
# =========================
APP_BASE = "https://ofmbot-production.up.railway.app"
GROUP_CHAT_ID = -1003046464831

WORKDIR = "/tmp/ofm_bot"
ADMINS = {684983417}                 # kerak bo‘lsa qo‘shimcha admin id’larni qo‘shing
ADMIN_WEB_KEY = "ofm"                # /admin?key=ofm; xohlasa o‘zgartiring

# =========================
# GLOBAL (RAM)
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

ACTIVE_USERS = set()
COUNTERS: Dict[str, int] = {
    "resume": 0, "convert": 0, "merge": 0, "split": 0,
    "ocr": 0, "pagenum": 0, "watermark": 0, "translate": 0
}
PENDING: Dict[int, dict] = {}
LAST_FILE: Dict[int, str] = {}

PAUSED = False
STARTED_AT = datetime.utcnow()

# =========================
# UTIL
# =========================
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def user_dir(uid: int) -> str:
    d = os.path.join(WORKDIR, str(uid))
    ensure_dir(d); return d

def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\.\-]+", "_", (name or "").strip())
    return name or f"file_{datetime.utcnow().timestamp():.0f}"

def save_bytes(path: str, data: bytes) -> str:
    ensure_dir(os.path.dirname(path))
    with open(path, "wb") as f: f.write(data)
    return path

def now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def human_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    units = ["KB","MB","GB","TB"]; i = int(math.log(n, 1024))
    return f"{n/(1024**i):.1f} {units[i]}"

# =========================
# KEYBOARDS (Reply)
# =========================
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="🆕 Rezyume"), KeyboardButton(text="🌐 Tarjima")],
            [KeyboardButton(text="🔄 Konvert"), KeyboardButton(text="📎 Birlashtirish"), KeyboardButton(text="✂️ Ajratish")],
            [KeyboardButton(text="🔢 Raqamlash"), KeyboardButton(text="💧 Watermark"), KeyboardButton(text="🔎 OCR")],
            [KeyboardButton(text="ℹ️ Yordam")],
        ],
    )

def kb_session(op: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor"), KeyboardButton(text="📋 Holat")],
            [KeyboardButton(text="🆕 Rezyume"), KeyboardButton(text="🌐 Tarjima")],
            [KeyboardButton(text="↩️ Asosiy menyu")],
        ],
    )

def kb_convert_targets() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="🎯 Target: PDF"), KeyboardButton(text="🎯 Target: PNG")],
            [KeyboardButton(text="🎯 Target: DOCX"), KeyboardButton(text="🎯 Target: PPTX")],
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")],
            [KeyboardButton(text="↩️ Asosiy menyu")],
        ],
    )

def kb_translate_targets(cur: str = "uz") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="🎯 Tgt: uz"), KeyboardButton(text="🎯 Tgt: en"), KeyboardButton(text="🎯 Tgt: ru")],
            [KeyboardButton(text=f"📌 Hozirgi: {cur}")],
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")],
            [KeyboardButton(text="↩️ Asosiy menyu")],
        ],
    )

def kb_webapp(id_val: int) -> InlineKeyboardMarkup:
    base = (APP_BASE or "").rstrip("/")
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Obyektivkani to‘ldirish",
                web_app=WebAppInfo(url=f"{base}/form?id={id_val}")
            )
        ]]
    )

# =========================
# FASTAPI (templates + admin)
# =========================
app = FastAPI()

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATES_DIR),
                  autoescape=select_autoescape(["html", "xml"]))

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    traceback.print_exc()
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)

@app.get("/", response_class=PlainTextResponse)
def root_ok(): return "OK"

@app.get("/admin", response_class=HTMLResponse)
def admin_page(key: str = "", pause: int = 0):
    global PAUSED
    if key == ADMIN_WEB_KEY and pause in (0,1):
        PAUSED = bool(pause)

    total = sum(COUNTERS.values())
    status_badge = '<span class="badge bg-success">ON</span>' if not PAUSED else '<span class="badge bg-danger">PAUSED</span>'
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k,v in COUNTERS.items())
    html = f"""
    <html><head>
    <title>OFM Admin</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    </head><body class="p-4">
    <h3>OFM — Admin panel {status_badge}</h3>
    <p>Uptime: {datetime.utcnow() - STARTED_AT}</p>
    <p>Foydalanuvchilar: <b>{len(ACTIVE_USERS)}</b> | Jami amallar: <b>{total}</b></p>
    <div class="mb-3">
      <a class="btn btn-danger" href="/admin?key={ADMIN_WEB_KEY}&pause=1">Pause</a>
      <a class="btn btn-success ms-2" href="/admin?key={ADMIN_WEB_KEY}&pause=0">Resume</a>
    </div>
    <table class="table table-bordered w-auto">
      <thead><tr><th>Funksiya</th><th>Soni</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </body></html>
    """
    return html

@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# =========================
# DOWNLOAD HELPERS
# =========================
async def grab_file_from_message(m: Message) -> Optional[str]:
    uid = m.from_user.id
    d = user_dir(uid)

    if m.document:
        f_id = m.document.file_id
        fn = safe_name(m.document.file_name or f"{now_stamp()}")
    elif m.photo:
        p = m.photo[-1]
        f_id = p.file_id
        fn = f"photo_{now_stamp()}.jpg"
    else:
        return None

    tg_file = await bot.get_file(f_id)
    local = os.path.join(d, fn)
    ensure_dir(d)
    await bot.download_file(tg_file.file_path, destination=local)
    return local

# =========================
# LOW-LEVEL CONVERTS
# =========================
def soffice_convert_to_pdf(src: str, out_dir: Optional[str] = None) -> str:
    if out_dir is None: out_dir = os.path.dirname(src)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", out_dir, src], check=True)
    base = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, base + ".pdf")
    if not os.path.exists(out): raise FileNotFoundError("LibreOffice conversion failed")
    return out

def images_to_single_pdf(img_paths: List[str], out_pdf: str) -> str:
    if not img_paths: raise ValueError("No images")
    imgs = [Image.open(p).convert("RGB") for p in img_paths]
    imgs[0].save(out_pdf, save_all=True, append_images=imgs[1:])
    return out_pdf

def pdf_merge(paths: List[str], out_pdf: str) -> str:
    wr = PdfWriter()
    for p in paths:
        rd = PdfReader(p)
        for pg in rd.pages: wr.add_page(pg)
    with open(out_pdf,"wb") as f: wr.write(f)
    return out_pdf

def pdf_split_range(src_pdf: str, rng: str, out_pdf: str) -> str:
    rd = PdfReader(src_pdf); wr = PdfWriter(); total = len(rd.pages)
    def add(ix): 
        if 1<=ix<=total: wr.add_page(rd.pages[ix-1])
    for part in re.split(r"\s*,\s*", rng.strip()):
        if not part: continue
        if "-" in part:
            a,b = part.split("-",1); a=int(a); b=int(b)
            step = 1 if a<=b else -1
            for i in range(a, b+step, step): add(i)
        else: add(int(part))
    with open(out_pdf,"wb") as f: wr.write(f)
    return out_pdf

def pdf_overlay_text(src_pdf: str, out_pdf: str, text: str, page_numbers: bool=False) -> str:
    rd = PdfReader(src_pdf); wr = PdfWriter()
    try:
        pdfmetrics.registerFont(TTFont("TimesNewRoman", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"))
        font = "TimesNewRoman"
    except Exception: font = "Helvetica"

    for i, page in enumerate(rd.pages, start=1):
        w = float(page.mediabox.width); h = float(page.mediabox.height)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(w,h))
        c.setFillAlpha(0.25); c.setFont(font, 28)
        if text:
            c.saveState(); c.translate(w/2, h/2); c.rotate(30)
            c.drawCentredString(0, 0, text); c.restoreState()
        if page_numbers:
            c.setFillAlpha(1.0); c.setFont(font, 12)
            c.drawString(w-60, 20, f"{i}/{len(rd.pages)}")
        c.save(); packet.seek(0)
        overlay = PdfReader(packet)
        page.merge_page(overlay.pages[0])
        wr.add_page(page)
    with open(out_pdf,"wb") as f: wr.write(f)
    return out_pdf

# =========================
# OCR
# =========================
def ocr_image(img_path: str) -> str:
    # lang bermaymiz -> tesseract default (o‘rnatilgan traineddata bo‘yicha auto)
    return pytesseract.image_to_string(Image.open(img_path)).strip()

def ocr_pdf(pdf_path: str, max_pages: int = 10, dpi: int = 200) -> str:
    pages = convert_from_path(pdf_path, dpi=dpi, fmt="jpeg", first_page=1, last_page=None)
    texts = []
    for i, im in enumerate(pages, start=1):
        if i > max_pages: break
        buf = io.BytesIO(); im.save(buf, format="JPEG"); buf.seek(0)
        txt = pytesseract.image_to_string(Image.open(buf))
        if txt.strip(): texts.append(txt.strip())
    return "\n\n---\n\n".join(texts)

# =========================
# RESUME (form → docx/pdf) – 422 dan holi
# =========================
def convert_docx_bytes_to_pdf_bytes(docx_bytes: bytes) -> Optional[bytes]:
    with tempfile.TemporaryDirectory() as td:
        docx_path = os.path.join(td, "file.docx")
        pdf_path = os.path.join(td, "file.pdf")
        save_bytes(docx_path, docx_bytes)
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, docx_path], check=True)
            with open(pdf_path,"rb") as f: return f.read()
        except Exception:
            traceback.print_exc(); return None

@app.post("/send_resume_data")
async def send_resume_data(
    full_name: str = Form(""), phone: str = Form(""), tg_id: str = Form(""),
    birth_date: str = Form(""), birth_place: str = Form(""),
    nationality: str = Form("O‘zbek"), party_membership: str = Form("Yo‘q"),
    education: str = Form(""), university: str = Form(""),
    specialization: str = Form("Yo‘q"), ilmiy_daraja: str = Form("Yo‘q"),
    ilmiy_unvon: str = Form("Yo‘q"), languages: str = Form("Yo‘q"),
    dav_mukofoti: str = Form("Yo‘q"), deputat: str = Form("Yo‘q"),
    adresss: str = Form(""), current_position_date: str = Form(""),
    current_position_full: str = Form(""), work_experience: str = Form(""),
    relatives: str = Form("[]"), photo: UploadFile | None = None,
):
    # Bo‘sh forma – xato qaytarmaymiz
    if not any([full_name, phone, birth_date, birth_place, education, university, specialization, work_experience, (photo and photo.filename)]):
        return {"status":"success","empty":True}

    try: rels = json.loads(relatives) if relatives else []
    except Exception: rels = []

    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx topilmadi"}, status_code=200)

    doc = DocxTemplate(tpl_path)
    ctx = {
        "full_name": full_name, "phone": phone, "birth_date": birth_date, "birth_place": birth_place,
        "nationality": nationality, "party_membership": party_membership, "education": education,
        "university": university, "specialization": specialization, "ilmiy_daraja": ilmiy_daraja,
        "ilmiy_unvon": ilmiy_unvon, "languages": languages, "dav_mukofoti": dav_mukofoti,
        "deputat": deputat, "adresss": adresss, "current_position_date": current_position_date,
        "current_position_full": current_position_full, "work_experience": work_experience, "relatives": rels,
    }

    inline_img = None; img_bytes = None
    if photo and photo.filename:
        try:
            img_bytes = await photo.read()
            inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
        except Exception: inline_img = None
    ctx["photo"] = inline_img

    buf = io.BytesIO(); doc.render(ctx); doc.save(buf); docx_bytes = buf.getvalue()
    pdf_bytes = convert_docx_bytes_to_pdf_bytes(docx_bytes)

    base = "_".join((full_name or "user").split()) or "user"
    docx_name = f"{base}_0.docx"; pdf_name = f"{base}_0.pdf"

    try:
        payload = {k:v for k,v in ctx.items() if k!="photo"}; payload["timestamp"] = datetime.utcnow().isoformat()+"Z"
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await bot.send_document(GROUP_CHAT_ID, BufferedInputFile(json_bytes, filename=f"{base}.json"),
                                caption=f"📄 Ma'lumotlar JSON: {full_name or '—'}")
        if img_bytes:
            await bot.send_document(GROUP_CHAT_ID, BufferedInputFile(img_bytes, filename=f"{base}.jpg"),
                                    caption=f"🖼 Foto: {full_name or '—'}")
    except Exception: traceback.print_exc()

    try:
        if tg_id:
            cid = int(tg_id)
            await bot.send_document(cid, BufferedInputFile(docx_bytes, filename=docx_name), caption="✅ Word format")
            if pdf_bytes:
                await bot.send_document(cid, BufferedInputFile(pdf_bytes, filename=pdf_name), caption="✅ PDF format")
    except Exception: traceback.print_exc()

    COUNTERS["resume"] += 1
    return {"status":"success"}

# =========================
# BOT COMMANDS / COMMON
# =========================
async def set_bot_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="Asosiy menyu"),
        BotCommand(command="new_resume", description="Obyektivka web-formasi"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="convert", description="Konvert"),
        BotCommand(command="merge", description="PDF birlashtirish"),
        BotCommand(command="split", description="PDF ajratish"),
        BotCommand(command="ocr", description="OCR"),
        BotCommand(command="pagenum", description="Sahifa raqamlash"),
        BotCommand(command="watermark", description="Watermark"),
        BotCommand(command="translate", description="Tarjima"),
    ])

def session_start(uid: int, op: str, seed: Optional[dict]=None):
    PENDING[uid] = {"op": op, "files": [], "params": seed or {}, "target": seed.get("target","") if seed else ""}

def session_clear(uid: int): PENDING.pop(uid, None)

def session_status_text(s: dict) -> str:
    parts = [f"🧰 Jarayon: {s['op']}", f"📁 Fayllar: {len(s['files'])}"]
    if s.get("target"): parts.append(f"🎯 Target: {s['target']}")
    if s["params"]: parts.append(f"⚙️ Parametrlar: {s['params']}")
    return "\n".join(parts)

def is_paused(uid: int) -> bool:
    return PAUSED and uid not in ADMINS

# =========================
# HANDLERS
# =========================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    await m.answer(
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "Fayl yuboring — mos amallarni taklif qilaman, yoki menyudan tanlang.",
        reply_markup=kb_main()
    )

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "📌 Qisqa qo‘llanma:\n"
        "• 🔄 Konvert: DOCX/PPTX/XLSX → PDF; rasm(lar) → PDF.\n"
        "• 📎 Birlashtirish: bir nechta PDF’ni bitta faylga.\n"
        "• ✂️ Ajratish: 1-3,5 kabi diapazon.\n"
        "• 🔎 OCR: rasm/PDF’dan matn (auto-lang).\n"
        "• 🔢 Raqamlash / 💧 Watermark: PDF ustida yozuv.\n"
        "• 🌐 Tarjima: matn/rasm/PDF → uz/en/ru."
    )

@dp.message(Command("new_resume"))
async def cmd_resume_command(m: Message):
    await show_resume_button(m)

async def show_resume_button(m: Message):
    base = (APP_BASE or "").rstrip("/")
    await m.answer(
        "👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
        "✅ Tez\n✅ Oson\n✅ Ishonchli\nquyidagi 🌐 web formani to'ldiring 👇",
        reply_markup=kb_main()
    )
    await m.answer("Obyektivkani to‘ldirish:", reply_markup=kb_webapp(m.from_user.id))

# ---- Session starters (buttons + slash) ----
@dp.message(lambda m: m.text in ["🔄 Konvert", "/convert"])
async def start_convert(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat ko‘rsatilmoqda.")
    session_start(m.from_user.id, "convert")
    await m.answer(
        "🔄 Konvert.\n1) Fayl(lar) yuboring (DOCX/PPTX/XLSX/PDF/rasm).\n"
        "2) Maqsad formatini tanlang.\n3) ✅ Yakunlash.",
        reply_markup=kb_convert_targets(),
    )

@dp.message(lambda m: m.text in ["📎 Birlashtirish", "/merge"])
async def start_merge(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    session_start(m.from_user.id, "merge")
    await m.answer("📎 PDF’larni yuboring, so‘ng ✅ Yakunlash.", reply_markup=kb_session("merge"))

@dp.message(lambda m: m.text in ["✂️ Ajratish", "/split"])
async def start_split(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    session_start(m.from_user.id, "split")
    await m.answer("✂️ Bitta PDF yuboring, keyin '1-3,5' kabi diapazon yozing va ✅ Yakunlash.", reply_markup=kb_session("split"))

@dp.message(lambda m: m.text in ["🔢 Raqamlash", "/pagenum"])
async def start_pagenum(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    session_start(m.from_user.id, "pagenum")
    await m.answer("🔢 Bitta PDF yuboring va ✅ Yakunlash.", reply_markup=kb_session("pagenum"))

@dp.message(lambda m: m.text in ["💧 Watermark", "/watermark"])
async def start_watermark(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    session_start(m.from_user.id, "watermark")
    await m.answer("💧 Bitta PDF yuboring. Keyin watermark matnini yozing va ✅ Yakunlash.", reply_markup=kb_session("watermark"))

@dp.message(lambda m: m.text in ["🔎 OCR", "/ocr"])
async def start_ocr(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    session_start(m.from_user.id, "ocr")
    await m.answer("🔎 Rasm yoki PDF yuboring, so‘ng ✅ Yakunlash.", reply_markup=kb_session("ocr"))

@dp.message(lambda m: m.text in ["🌐 Tarjima", "/translate"])
async def start_translate(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    session_start(m.from_user.id, "translate", seed={"tgt":"uz"})
    await m.answer(
        "🌐 Tarjima. Matn yuboring yoki rasm/PDF yuboring (OCR orqali). Tillarni tanlang.",
        reply_markup=kb_translate_targets("uz")
    )

# ---- Target choosers ----
@dp.message(lambda m: m.text and m.text.startswith("🎯 Target:"))
async def set_target(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)
    if not s or s["op"] != "convert": return
    tgt = m.text.split(":",1)[1].strip().lower()
    if tgt in ["pdf","png","docx","pptx"]:
        s["target"] = tgt
        await m.answer(f"🎯 Target: {tgt.upper()}")

@dp.message(lambda m: m.text and m.text.startswith("🎯 Tgt:"))
async def set_translate_tgt(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)
    if not s or s["op"] != "translate": return
    tgt = m.text.split(":",1)[1].strip().lower()
    if tgt in ["uz","en","ru"]:
        s["params"]["tgt"] = tgt
        await m.answer(f"🎯 Target til: {tgt}", reply_markup=kb_translate_targets(tgt))

# ---- Session control ----
@dp.message(lambda m: m.text in ["❌ Bekor", "/cancel"])
async def cancel_session(m: Message):
    session_clear(m.from_user.id)
    await m.answer("❌ Session bekor qilindi.", reply_markup=kb_main())

@dp.message(lambda m: m.text in ["📋 Holat", "/status"])
async def status_session(m: Message):
    s = PENDING.get(m.from_user.id)
    if not s: return await m.answer("ℹ️ Aktiv session yo‘q.", reply_markup=kb_main())
    await m.answer("📄\n" + session_status_text(s))

@dp.message(lambda m: m.text == "↩️ Asosiy menyu")
async def back_to_main(m: Message):
    await m.answer("Asosiy menyu", reply_markup=kb_main())

# ---- File receiver ----
@dp.message(lambda m: bool(m.document) or bool(m.photo))
async def any_file_received(m: Message):
    if is_paused(m.from_user.id): return await m.answer("⏸ Texnik xizmat.")
    local = await grab_file_from_message(m)
    if not local: return await m.answer("❌ Faylni yuklab bo‘lmadi.")
    LAST_FILE[m.from_user.id] = local

    s = PENDING.get(m.from_user.id)
    if s:
        s["files"].append(local)
        await m.answer(f"📥 Qabul qilindi: {os.path.basename(local)} ({human_size(os.path.getsize(local))})")
    else:
        await m.answer(
            "📎 Fayl qabul qilindi. Quyidagidan birini tanlang:",
            reply_markup=ReplyKeyboardMarkup(
                resize_keyboard=True,
                keyboard=[
                    [KeyboardButton(text="🔄 Konvert"), KeyboardButton(text="📎 Birlashtirish"), KeyboardButton(text="🔎 OCR")],
                    [KeyboardButton(text="🌐 Tarjima"), KeyboardButton(text="↩️ Asosiy menyu")],
                ],
            ),
        )

# ---- Finalize (/done) ----
@dp.message(lambda m: m.text in ["✅ Yakunlash", "/done"])
async def finalize(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)
    if not s: return await m.answer("ℹ️ Aktiv session yo‘q.", reply_markup=kb_main())

    op = s["op"]; files = s["files"]; params = s["params"]; tgt = s.get("target")
    out_dir = user_dir(uid)

    try:
        if op == "convert":
            if not files: return await m.answer("Fayl yuboring.")
            if not tgt: return await m.answer("🎯 Target tanlang.")

            result_paths: List[str] = []
            if tgt == "pdf":
                imgs = [p for p in files if os.path.splitext(p)[1].lower() in [".jpg",".jpeg",".png",".webp"]]
                others = [p for p in files if p not in imgs]
                if imgs:
                    out = os.path.join(out_dir, f"images_{now_stamp()}.pdf")
                    images_to_single_pdf(imgs, out); result_paths.append(out)
                for f in others:
                    out = soffice_convert_to_pdf(f, out_dir); result_paths.append(out)
                if len(result_paths) > 1:
                    merged = os.path.join(out_dir, f"merged_{now_stamp()}.pdf")
                    pdf_merge(result_paths, merged); result_paths = [merged]

            elif tgt == "png":
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext == ".pdf":
                        await m.answer("ℹ️ PDF → PNG uchun rasterlash o‘rnatilmagan (poppler bilan to‘liq eksport qilishni yoqsangiz bo‘ladi).")
                    else:
                        im = Image.open(f).convert("RGB")
                        out = os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + ".png")
                        im.save(out, format="PNG")
                        result_paths.append(out)

            elif tgt in ["docx","pptx"]:
                return await m.answer("⚠️ Bunday konvert hozircha qo‘llanmaydi.")

            for rp in result_paths:
                await bot.send_document(uid, BufferedInputFile(open(rp,"rb").read(), filename=os.path.basename(rp)))
            COUNTERS["convert"] += 1

        elif op == "merge":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if len(pdfs) < 2: return await m.answer("Kamida 2 ta PDF yuboring.")
            out = os.path.join(out_dir, f"merged_{now_stamp()}.pdf")
            pdf_merge(pdfs, out)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["merge"] += 1

        elif op == "split":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if not pdfs: return await m.answer("Bitta PDF yuboring.")
            rng = params.get("range")
            if not rng: return await m.answer("Diapazon kiriting (masalan: 1-3,5).")
            out = os.path.join(out_dir, f"split_{now_stamp()}.pdf")
            pdf_split_range(pdfs[0], rng, out)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["split"] += 1

        elif op == "pagenum":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if not pdfs: return await m.answer("Bitta PDF yuboring.")
            out = os.path.join(out_dir, f"pagenum_{now_stamp()}.pdf")
            pdf_overlay_text(pdfs[0], out, text="", page_numbers=True)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["pagenum"] += 1

        elif op == "watermark":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if not pdfs: return await m.answer("Bitta PDF yuboring.")
            wm = params.get("wm_text", "OFM")
            out = os.path.join(out_dir, f"watermark_{now_stamp()}.pdf")
            pdf_overlay_text(pdfs[0], out, text=wm, page_numbers=False)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["watermark"] += 1

        elif op == "ocr":
            if not files: return await m.answer("Rasm yoki PDF yuboring.")
            texts = []
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext == ".pdf":
                    try: texts.append(ocr_pdf(f))
                    except Exception: traceback.print_exc()
                else:
                    try: texts.append(ocr_image(f))
                    except Exception: traceback.print_exc()
            if texts:
                await m.answer("📝 OCR natija:\n\n" + "\n\n---\n\n".join([t for t in texts if t][:3]))
            else:
                await m.answer("Matn topilmadi.")
            COUNTERS["ocr"] += 1

        elif op == "translate":
            tgt = params.get("tgt", "uz")
            translator = Translator()
            texts = []
            # file bo‘lsa — OCR qilib olamiz; text bo‘lsa — to‘g‘ridan
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext == ".pdf":
                    try: texts.append(ocr_pdf(f))
                    except Exception: traceback.print_exc()
                elif ext in [".jpg",".jpeg",".png",".webp"]:
                    try: texts.append(ocr_image(f))
                    except Exception: traceback.print_exc()
            if (not files) and m.text and m.text not in ["✅ Yakunlash","❌ Bekor","📋 Holat","↩️ Asosiy menyu"]:
                texts.append(m.text)

            full = "\n\n".join([t for t in texts if t])
            if not full.strip(): return await m.answer("Tarjima uchun matn yo‘q.")
            tr = translator.translate(full, dest=tgt)
            await m.answer(f"🌐 Tarjima → {tgt}:\n\n{tr.text[:4000]}")
            COUNTERS["translate"] += 1

        await m.answer("✅ Yakunlandi.", reply_markup=kb_main())
        session_clear(uid)

    except Exception as e:
        traceback.print_exc()
        await m.answer(f"❌ Xatolik: {e}", reply_markup=kb_main())
        session_clear(uid)

# ---- Parametrlar: split diapazon / watermark matn ----
GLOBAL_BUTTONS = {
    "🆕 Rezyume": "resume", "🌐 Tarjima": "translate", "↩️ Asosiy menyu": "main",
    "🔄 Konvert": "convert", "📎 Birlashtirish": "merge", "✂️ Ajratish": "split",
    "🔢 Raqamlash": "pagenum", "💧 Watermark": "watermark", "🔎 OCR": "ocr",
    "ℹ️ Yordam": "help"
}

@dp.message(lambda m: True)
async def free_text_router(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)

    # Global tugmalar sessionni bosib ketmasin:
    txt = (m.text or "").strip()
    if txt in GLOBAL_BUTTONS:
        key = GLOBAL_BUTTONS[txt]
        if key == "resume":  return await show_resume_button(m)
        if key == "translate": return await start_translate(m)
        if key == "main":    return await back_to_main(m)
        if key == "convert": return await start_convert(m)
        if key == "merge":   return await start_merge(m)
        if key == "split":   return await start_split(m)
        if key == "pagenum": return await start_pagenum(m)
        if key == "watermark": return await start_watermark(m)
        if key == "ocr":     return await start_ocr(m)
        if key == "help":    return await cmd_help(m)

    if not s:
        return  # hech narsa qilmeymiz

    op = s["op"]
    if op == "split":
        if re.fullmatch(r"[\d,\-\s]+", txt):
            s["params"]["range"] = txt
            await m.answer(f"📌 Diapazon: {txt}")
    elif op == "watermark":
        if txt and txt not in ["✅ Yakunlash","❌ Bekor","📋 Holat"]:
            s["params"]["wm_text"] = txt
            await m.answer(f"📌 Watermark: {txt}")
    elif op == "translate":
        # oddiy matn — tarjima tarkibiga qo‘shib turamiz (yakunlashda foydalanamiz)
        if txt and txt not in ["✅ Yakunlash","❌ Bekor","📋 Holat"]:
            # matnni vaqtincha fayllarsiz saqlamaymiz; finalize vaqtida m.text ham olinadi
            await m.answer("✍️ Matn qabul qilindi. Kerak bo‘lsa rasm/PDF ham yuboring yoki ✅ Yakunlash bosing.")

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
        traceback.print_exc()
        print("Update JSON:", data, file=sys.stderr)
        return {"ok": False}

@app.get("/bot/set_webhook")
async def set_webhook(base: str | None = None):
    base_url = (base or APP_BASE).rstrip("/")
    await set_bot_commands()
    await bot.set_webhook(f"{base_url}/bot/webhook")
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}

# =========================
# STARTUP
# =========================
@app.on_event("startup")
async def on_startup():
    ensure_dir(WORKDIR)
    try: await set_bot_commands()
    except Exception: traceback.print_exc()
