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
from typing import Optional, Tuple, List

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
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"  # trailing slashsiz
GROUP_CHAT_ID = -1003046464831  # JSON & rasm boradigan guruh

WORKDIR = "/tmp/ofm_bot"

# =========================
# GLOBALS (RAM)
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

ACTIVE_USERS = set()
COUNTERS = {
    "resume": 0,
    "convert": 0,
    "merge": 0,
    "split": 0,
    "ocr": 0,
    "pagenum": 0,
    "watermark": 0,
}
# foydalanuvchi sessiyalari
PENDING: dict[int, dict] = {}   # {uid: {"op": str, "files": [paths], "params": {...}, "target": str}}
LAST_FILE: dict[int, str] = {}  # oxirgi yuborilgan fayl (tezkortaqdim uchun)

# =========================
# UTIL
# =========================
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def user_dir(uid: int) -> str:
    d = os.path.join(WORKDIR, str(uid))
    ensure_dir(d)
    return d

def safe_name(name: str) -> str:
    name = re.sub(r"[^\w\.\-]+", "_", name.strip())
    return name or "file"

def save_bytes(path: str, data: bytes) -> str:
    ensure_dir(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(data)
    return path

def now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def human_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    k = 1024.0
    sizes = ["KB","MB","GB","TB"]
    i = int(math.floor(math.log(n, k)))
    return f"{n / (k**i):.1f} {sizes[i]}"

# =========================
# KEYBOARDS
# =========================
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="🆕 Rezyume"), KeyboardButton(text="🔄 Konvert"), KeyboardButton(text="📎 Birlashtirish")],
            [KeyboardButton(text="✂️ Ajratish"), KeyboardButton(text="🔢 Raqamlash"), KeyboardButton(text="💧 Watermark")],
            [KeyboardButton(text="🔎 OCR"), KeyboardButton(text="🌐 Tarjima")],
            [KeyboardButton(text="ℹ️ Yordam")],
        ],
    )

def kb_session(op: str) -> ReplyKeyboardMarkup:
    tag = {
        "convert": "Konvert",
        "merge": "Birlashtirish",
        "split": "Ajratish",
        "pagenum": "Raqamlash",
        "watermark": "Watermark",
        "ocr": "OCR",
        "translate": "Tarjima",
    }.get(op, "Jarayon")
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")],
            [KeyboardButton(text="📋 Holat")],
            [KeyboardButton(text=f"↩️ Asosiy menyu ({tag})")],
        ],
    )

def kb_convert_targets() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="🎯 Target: PDF"), KeyboardButton(text="🎯 Target: PNG")],
            [KeyboardButton(text="🎯 Target: DOCX"), KeyboardButton(text="🎯 Target: PPTX")],
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")],
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
# TEMPLATES (FastAPI)
# =========================
app = FastAPI()

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print("=== GLOBAL ERROR ===", file=sys.stderr)
    traceback.print_exc()
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)

@app.get("/", response_class=PlainTextResponse)
def root_ok(): return "OK"

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    total = sum(COUNTERS.values())
    body = f"""
    <html><head>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <title>OFM Admin</title></head>
    <body class="p-4">
    <h3>OFM — Mini Dashboard</h3>
    <p>Foydalanuvchilar: <b>{len(ACTIVE_USERS)}</b></p>
    <p>Jami amallar: <b>{total}</b></p>
    <table class="table table-bordered w-auto">
      <tr><th>Funksiya</th><th>Sana</th><th>Hisob</th></tr>
      {''.join(f"<tr><td>{k}</td><td>{datetime.utcnow().date()}</td><td>{v}</td></tr>" for k,v in COUNTERS.items())}
    </table>
    </body></html>
    """
    return body

@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# =========================
# DOWNLOAD HELPERS
# =========================
async def grab_file_from_message(m: Message) -> Optional[str]:
    """
    Telegram dan faylni userning vaqtinchalik papkasiga tushirib, lokal yo‘lini qaytaradi.
    Rasm (photo) ham, document ham ishlaydi.
    """
    uid = m.from_user.id
    d = user_dir(uid)

    if m.document:
        f_id = m.document.file_id
        fn = safe_name(m.document.file_name or f"{now_stamp()}")
    elif m.photo:
        # eng kattasini olamiz
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
# CONVERTERS
# =========================
def soffice_convert_to_pdf(src: str, out_dir: Optional[str] = None) -> str:
    """LibreOffice bilan har qanday (docx, xlsx, pptx, …) -> PDF"""
    if out_dir is None:
        out_dir = os.path.dirname(src)
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", out_dir, src],
        check=True
    )
    base = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, base + ".pdf")
    if not os.path.exists(out):
        raise FileNotFoundError("LibreOffice conversion failed")
    return out

def images_to_single_pdf(img_paths: List[str], out_pdf: str) -> str:
    """Bir nechta rasmdan bitta PDF yasash (Pillow)."""
    if not img_paths:
        raise ValueError("No images")
    pil_imgs = []
    for p in img_paths:
        im = Image.open(p).convert("RGB")
        pil_imgs.append(im)
    first, rest = pil_imgs[0], pil_imgs[1:]
    first.save(out_pdf, save_all=True, append_images=rest)
    return out_pdf

def pdf_merge_bytes(paths: List[str], out_pdf: str) -> str:
    wr = PdfWriter()
    for p in paths:
        rd = PdfReader(p)
        for pg in rd.pages:
            wr.add_page(pg)
    with open(out_pdf, "wb") as f:
        wr.write(f)
    return out_pdf

def pdf_split_range(src_pdf: str, rng: str, out_pdf: str) -> str:
    """
    rng masalan: "1-3,5,7-7"
    """
    rd = PdfReader(src_pdf)
    wr = PdfWriter()
    total = len(rd.pages)

    def add_page(ix):
        if 1 <= ix <= total:
            wr.add_page(rd.pages[ix-1])

    for part in re.split(r"\s*,\s*", rng.strip()):
        if not part: continue
        if "-" in part:
            a,b = part.split("-",1)
            a = int(a); b = int(b)
            if a<=b:
                for i in range(a,b+1): add_page(i)
            else:
                for i in range(a,b-1,-1): add_page(i)
        else:
            add_page(int(part))

    with open(out_pdf,"wb") as f: wr.write(f)
    return out_pdf

def pdf_overlay_text(src_pdf: str, out_pdf: str, text: str, page_numbers: bool=False) -> str:
    """Watermark yoki sahifa raqamlash (ReportLab)."""
    rd = PdfReader(src_pdf)
    wr = PdfWriter()
    try:
        pdfmetrics.registerFont(TTFont("TimesNewRoman", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"))
        font_name = "TimesNewRoman"
    except Exception:
        font_name = "Helvetica"

    for i, page in enumerate(rd.pages, start=1):
        w = float(page.mediabox.width); h = float(page.mediabox.height)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(w,h))
        c.setFillAlpha(0.25)
        c.setFont(font_name, 28)
        if text:
            c.saveState()
            c.translate(w/2, h/2)
            c.rotate(30)
            c.drawCentredString(0, 0, text)
            c.restoreState()
        if page_numbers:
            c.setFont(font_name, 12)
            c.setFillAlpha(1.0)
            c.drawString(w-60, 20, f"{i}/{len(rd.pages)}")
        c.save()
        packet.seek(0)
        overlay = PdfReader(packet)
        page.merge_page(overlay.pages[0])
        wr.add_page(page)
    with open(out_pdf,"wb") as f: wr.write(f)
    return out_pdf

# =========================
# OCR
# =========================
def run_ocr_on_image(img_path: str) -> str:
    import pytesseract
    img = Image.open(img_path)
    # tilni ko‘rsatmasak — tesseract o‘zi tanlaydi (mavjud traineddata’ga ko‘ra)
    txt = pytesseract.image_to_string(img)
    return txt.strip()

def ocr_pdf_to_text(pdf_path: str) -> str:
    # oddiy usul: har sahifani rasterlab olib OCR
    # (poppler-utils o‘rnatilgan bo‘lsa tezroq bo‘ladi, lekin Pillow ham ishlaydi)
    import fitz  # PyMuPDF kerak emas – ishlatmaymiz; oddiy raster: Pillow + pdf -> rasmlar
    raise NotImplementedError  # soddalashtirish: foydalanuvchi rasm yuborganida to‘liq ishlaydi

# =========================
# RESUME (form → docx/pdf)
# =========================
def convert_docx_bytes_to_pdf_bytes(docx_bytes: bytes) -> Optional[bytes]:
    with tempfile.TemporaryDirectory() as td:
        docx_path = os.path.join(td, "file.docx")
        pdf_path = os.path.join(td, "file.pdf")
        save_bytes(docx_path, docx_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, docx_path],
                check=True
            )
            with open(pdf_path,"rb") as f:
                return f.read()
        except Exception:
            traceback.print_exc()
            return None

@app.post("/send_resume_data")
async def send_resume_data(
    full_name: str = Form(""),
    phone: str = Form(""),
    tg_id: str = Form(""),
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
    # agar butunlay bo‘sh bo‘lsa – xatosiz qaytamiz
    if not any([full_name, phone, birth_date, birth_place, education, university, specialization, work_experience, (photo and photo.filename)]):
        return {"status":"success","empty":True}

    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

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
        "current_position_full": current_position_full, "work_experience": work_experience,
        "relatives": rels,
    }

    inline_img = None
    if photo and photo.filename:
        try:
            img_bytes = await photo.read()
            inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
        except Exception:
            inline_img = None
    ctx["photo"] = inline_img

    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()
    pdf_bytes = convert_docx_bytes_to_pdf_bytes(docx_bytes)

    # nomlar
    base = "_".join((full_name or "user").split()) or "user"
    docx_name = f"{base}_0.docx"
    pdf_name  = f"{base}_0.pdf"

    # guruhga json + rasm
    try:
        payload = {k:v for k,v in ctx.items() if k != "photo"}
        payload["timestamp"] = datetime.utcnow().isoformat() + "Z"
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await bot.send_document(GROUP_CHAT_ID, BufferedInputFile(json_bytes, filename=f"{base}.json"),
                                caption=f"📄 Ma'lumotlar JSON: {full_name or '—'}")
        if photo and photo.filename:
            await bot.send_document(GROUP_CHAT_ID, BufferedInputFile(img_bytes, filename=f"{base}.jpg"),
                                    caption=f"🖼 Foto: {full_name or '—'}")
    except Exception:
        traceback.print_exc()

    # foydalanuvchiga
    try:
        if tg_id:
            cid = int(tg_id)
            await bot.send_document(cid, BufferedInputFile(docx_bytes, filename=docx_name),
                                    caption="✅ Word formatdagi rezyume")
            if pdf_bytes:
                await bot.send_document(cid, BufferedInputFile(pdf_bytes, filename=pdf_name),
                                        caption="✅ PDF formatdagi rezyume")
    except Exception:
        traceback.print_exc()

    COUNTERS["resume"] += 1
    return {"status":"success"}

# =========================
# BOT COMMANDS & HELPERS
# =========================
async def set_bot_commands():
    cmds = [
        BotCommand(command="start", description="Asosiy menyu"),
        BotCommand(command="new_resume", description="Obyektivka web-formasi"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="convert", description="Konvert sessiyasi"),
        BotCommand(command="merge", description="PDF birlashtirish"),
        BotCommand(command="split", description="PDF ajratish"),
        BotCommand(command="ocr", description="OCR — matn ajratish"),
        BotCommand(command="pagenum", description="PDF sahifa raqamlash"),
        BotCommand(command="watermark", description="PDF watermark"),
    ]
    await bot.set_my_commands(cmds)

def session_start(uid: int, op: str):
    PENDING[uid] = {"op": op, "files": [], "params": {}, "target": ""}

def session_clear(uid: int):
    PENDING.pop(uid, None)

def session_status_text(s: dict) -> str:
    parts = [f"🧰 Jarayon: {s['op']}"]
    parts.append(f"📁 Fayllar: {len(s['files'])}")
    if s.get("target"):
        parts.append(f"🎯 Target: {s['target']}")
    if s["params"]:
        parts.append(f"⚙️ Parametrlar: {s['params']}")
    return "\n".join(parts)

# =========================
# HANDLERS
# =========================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    text = (
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "Quyidagi menyudan tanlang yoki fayl yuboring — mos amallarni taklif qilaman."
    )
    await m.answer(text, reply_markup=kb_main())

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "📌 Qisqa qo‘llanma:\n"
        "• 🔄 Konvert: DOCX/PPTX/XLSX → PDF; rasm(lar) → PDF va h.k.\n"
        "• 📎 Birlashtirish: bir nechta PDF’ni bitta faylga.\n"
        "• ✂️ Ajratish: PDF’dan sahifalarni ajratish (masalan 1-3,5).\n"
        "• 🔎 OCR: rasm/PDF’dan matn chiqarish (auto lang).\n"
        "• 🔢 Raqamlash, 💧 Watermark: PDF ustiga yozish.\n"
        "• 🆕 Rezyume: WebApp forma orqali obyektivka tayyorlash."
    )

@dp.message(Command("new_resume"))
async def cmd_resume(m: Message):
    base = (APP_BASE or "").rstrip("/")
    await m.answer(
        "👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
        "✅ Tez\n✅ Oson\n✅ Ishonchli\n"
        "quyidagi 🌐 web formani to'ldiring 👇",
        reply_markup=kb_main()
    )
    await m.answer("Obyektivkani to‘ldirish:", reply_markup=None)
    await m.answer("➡️ WebApp'ni ochish uchun tugma", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Obyektivkani to‘ldirish",
                web_app=WebAppInfo(url=f"{base}/form?id={m.from_user.id}")
            )
        ]]
    ))

# --- Session start buttons ---
@dp.message(lambda m: m.text in ["🔄 Konvert", "/convert"])
async def start_convert(m: Message):
    session_start(m.from_user.id, "convert")
    await m.answer(
        "🔄 Konvert sessiyasi boshlandi.\n"
        "1) Fayl(lar) yuboring (DOCX/PPTX/XLSX yoki PDF; rasm(lar) → PDF uchun rasm yuboring).\n"
        "2) Maqsad formatini tanlang (quyidagi tugmalar).\n"
        "3) ✅ Yakunlash.",
        reply_markup=kb_convert_targets()
    )

@dp.message(lambda m: m.text in ["📎 Birlashtirish", "/merge"])
async def start_merge(m: Message):
    session_start(m.from_user.id, "merge")
    await m.answer(
        "📎 Birlashtirish boshlandi.\nPDF fayllarni yuboring, so‘ng ✅ Yakunlash.",
        reply_markup=kb_session("merge")
    )

@dp.message(lambda m: m.text in ["✂️ Ajratish", "/split"])
async def start_split(m: Message):
    session_start(m.from_user.id, "split")
    await m.answer(
        "✂️ Ajratish.\nBitta PDF yuboring, keyin ajratish diapazonini yozing (masalan: 1-3,5), so‘ng ✅ Yakunlash.",
        reply_markup=kb_session("split")
    )

@dp.message(lambda m: m.text in ["🔢 Raqamlash", "/pagenum"])
async def start_pagenum(m: Message):
    session_start(m.from_user.id, "pagenum")
    await m.answer("🔢 Raqamlash.\nBitta PDF yuboring, so‘ng ✅ Yakunlash.", reply_markup=kb_session("pagenum"))

@dp.message(lambda m: m.text in ["💧 Watermark", "/watermark"])
async def start_watermark(m: Message):
    session_start(m.from_user.id, "watermark")
    await m.answer("💧 Watermark.\nBitta PDF yuboring. Keyin watermark matnini yozing va ✅ Yakunlash.", reply_markup=kb_session("watermark"))

@dp.message(lambda m: m.text in ["🔎 OCR", "/ocr"])
async def start_ocr(m: Message):
    session_start(m.from_user.id, "ocr")
    await m.answer("🔎 OCR.\nRasm yoki PDF yuboring, so‘ng ✅ Yakunlash.", reply_markup=kb_session("ocr"))

# --- Target buttons for convert ---
@dp.message(lambda m: m.text and m.text.startswith("🎯 Target:"))
async def set_target(m: Message):
    uid = m.from_user.id
    if uid not in PENDING or PENDING[uid]["op"] != "convert":
        return
    tgt = m.text.split(":",1)[1].strip().lower()
    if tgt in ["pdf","png","docx","pptx"]:
        PENDING[uid]["target"] = tgt
        await m.answer(f"🎯 Target qabul qilindi: {tgt.upper()}")

# --- Common session control ---
@dp.message(lambda m: m.text in ["❌ Bekor", "/cancel"])
async def cancel_session(m: Message):
    uid = m.from_user.id
    if uid in PENDING: session_clear(uid)
    await m.answer("❌ Session bekor qilindi.", reply_markup=kb_main())

@dp.message(lambda m: m.text in ["📋 Holat", "/status"])
async def status_session(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)
    if not s:
        await m.answer("ℹ️ Aktiv session yo‘q.", reply_markup=kb_main())
        return
    await m.answer("📄\n" + session_status_text(s))

@dp.message(lambda m: m.text and m.text.startswith("↩️ Asosiy menyu"))
async def back_to_main(m: Message):
    await m.answer("Asosiy menyu", reply_markup=kb_main())

# --- Files receiver (works for any session) ---
@dp.message(lambda m: bool(m.document) or bool(m.photo))
async def any_file_received(m: Message):
    uid = m.from_user.id
    local = await grab_file_from_message(m)
    if not local:
        await m.answer("❌ Faylni yuklab bo‘lmadi.")
        return
    LAST_FILE[uid] = local

    s = PENDING.get(uid)
    if s:
        s["files"].append(local)
        size = os.path.getsize(local)
        await m.answer(f"📥 Fayl qabul qilindi: {os.path.basename(local)} ({human_size(size)})")
    else:
        # session yo‘q — takliflar
        await m.answer(
            "📎 Fayl qabul qilindi.\nQuyidagidan birini tanlang:",
            reply_markup=ReplyKeyboardMarkup(
                resize_keyboard=True,
                keyboard=[
                    [KeyboardButton(text="🔄 Konvert"), KeyboardButton(text="📎 Birlashtirish")],
                    [KeyboardButton(text="🔎 OCR"), KeyboardButton(text="✂️ Ajratish")],
                    [KeyboardButton(text="↩️ Asosiy menyu (Tezkor)")],
                ],
            )
        )

# --- Finalize ---
@dp.message(lambda m: m.text in ["✅ Yakunlash", "/done"])
async def finalize(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)
    if not s:
        await m.answer("ℹ️ Aktiv session yo‘q.", reply_markup=kb_main()); return

    op = s["op"]; files = s["files"]; params = s["params"]; tgt = s.get("target")
    out_dir = user_dir(uid)

    try:
        if op == "convert":
            if not files:
                await m.answer("PDF yig‘ish uchun mos fayl yo‘q."); return
            if not tgt:
                await m.answer("🎯 Target tanlanmagan."); return

            result_paths: List[str] = []

            if tgt == "pdf":
                # rasmlar bo‘lsa — bitta PDFga; boshqa formatlar bo‘lsa — LibreOffice
                imgs = [p for p in files if os.path.splitext(p)[1].lower() in [".jpg",".jpeg",".png",".webp"]]
                others = [p for p in files if p not in imgs]
                if imgs:
                    out = os.path.join(out_dir, f"images_{now_stamp()}.pdf")
                    images_to_single_pdf(imgs, out)
                    result_paths.append(out)
                for f in others:
                    out = soffice_convert_to_pdf(f, out_dir)
                    result_paths.append(out)
                # ko‘p natija chiqqan bo‘lsa — birlashtirib beramiz
                if len(result_paths) > 1:
                    merged = os.path.join(out_dir, f"merged_{now_stamp()}.pdf")
                    pdf_merge_bytes(result_paths, merged)
                    result_paths = [merged]

            elif tgt == "png":
                for f in files:
                    if os.path.splitext(f)[1].lower() == ".pdf":
                        rd = PdfReader(f)
                        for i, _ in enumerate(rd.pages, start=1):
                            # minimal stub: preview shartsiz, faqat info
                            # (rasmga aylantirish uchun poppler/pdf2image kerak; konteynerda bo‘lishi shart emas)
                            pass
                        await m.answer("ℹ️ PDF→PNG ustida to‘liq rasterlash o‘rnatilmagan. LibreOffice/Pillow bilan PDF→PNG ni qo‘llab-quvvatlash uchun poppler kerak.")
                    else:
                        im = Image.open(f).convert("RGB")
                        out = os.path.join(out_dir, f"{os.path.splitext(os.path.basename(f))[0]}.png")
                        im.save(out, format="PNG")
                        result_paths.append(out)

            elif tgt in ["docx","pptx"]:
                # png/jpg ni bevosita docx/pptx ga aylantirish mantiqan yo‘q; foydalanuvchiga ogohlantirish
                await m.answer("⚠️ Fayl(lar)ni bevosita DOCX/PPTX’ga aylantirish qo‘llab-quvvatlanmaydi.")
                return

            for rp in result_paths:
                await bot.send_document(uid, BufferedInputFile(open(rp,"rb").read(), filename=os.path.basename(rp)))
            COUNTERS["convert"] += 1

        elif op == "merge":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if len(pdfs) < 2:
                await m.answer("Kamida 2 ta PDF yuboring.")
                return
            out = os.path.join(out_dir, f"merged_{now_stamp()}.pdf")
            pdf_merge_bytes(pdfs, out)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["merge"] += 1

        elif op == "split":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if not pdfs:
                await m.answer("Bitta PDF yuboring.")
                return
            rng = params.get("range")
            if not rng:
                await m.answer("Diapazon kiritilmagan. Masalan: 1-3,5")
                return
            out = os.path.join(out_dir, f"split_{now_stamp()}.pdf")
            pdf_split_range(pdfs[0], rng, out)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["split"] += 1

        elif op == "pagenum":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if not pdfs:
                await m.answer("Bitta PDF yuboring."); return
            out = os.path.join(out_dir, f"pagenum_{now_stamp()}.pdf")
            pdf_overlay_text(pdfs[0], out, text="", page_numbers=True)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["pagenum"] += 1

        elif op == "watermark":
            pdfs = [p for p in files if p.lower().endswith(".pdf")]
            if not pdfs:
                await m.answer("Bitta PDF yuboring."); return
            wm = params.get("wm_text", "OFM")
            out = os.path.join(out_dir, f"watermark_{now_stamp()}.pdf")
            pdf_overlay_text(pdfs[0], out, text=wm, page_numbers=False)
            await bot.send_document(uid, BufferedInputFile(open(out,"rb").read(), filename=os.path.basename(out)))
            COUNTERS["watermark"] += 1

        elif op == "ocr":
            # hozircha rasm OCR
            imgs = [p for p in files if os.path.splitext(p)[1].lower() in [".jpg",".jpeg",".png",".webp"]]
            if not imgs:
                await m.answer("OCR uchun rasm yuboring (PDF OCR tez orada).")
                return
            all_txt = []
            for p in imgs:
                try:
                    txt = run_ocr_on_image(p)
                    if txt: all_txt.append(txt)
                except Exception:
                    traceback.print_exc()
            if all_txt:
                await m.answer("📝 OCR natija:\n\n" + "\n\n---\n\n".join(all_txt[:5]))
            else:
                await m.answer("Matn topilmadi.")
            COUNTERS["ocr"] += 1

        await m.answer("✅ Yakunlandi.", reply_markup=kb_main())
        session_clear(uid)

    except Exception as e:
        traceback.print_exc()
        await m.answer(f"❌ Xatolik: {e}", reply_markup=kb_main())
        session_clear(uid)

# --- Parameters input (range / wm text) ---
@dp.message(lambda m: True)
async def free_text_router(m: Message):
    uid = m.from_user.id
    s = PENDING.get(uid)
    if not s:
        # hech qanday session yo‘q — foydalanuvchini yo‘naltiramiz
        if m.text in ["ℹ️ Yordam"]:
            await cmd_help(m); return
        if m.text in ["🆕 Rezyume"]:
            await cmd_resume(m); return
        if m.text in ["🔄 Konvert", "📎 Birlashtirish", "✂️ Ajratish", "🔎 OCR","🔢 Raqamlash","💧 Watermark"]:
            # tegishli starterlar allaqachon handle qiladi
            return
        # boshqa hollarda asosiy menyu
        return

    op = s["op"]
    txt = (m.text or "").strip()

    if op == "split":
        # diapazon sifatida qabul qilamiz (validatsiya yengil)
        if re.fullmatch(r"[\d,\-\s]+", txt):
            s["params"]["range"] = txt
            await m.answer(f"📌 Diapazon: {txt}")
    elif op == "watermark":
        if txt and txt not in ["✅ Yakunlash","❌ Bekor","📋 Holat"]:
            s["params"]["wm_text"] = txt
            await m.answer(f"📌 Watermark: {txt}")

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
    try:
        await set_bot_commands()
    except Exception:
        traceback.print_exc()
