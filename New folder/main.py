# app/main.py
# -----------------------------------------------------------
# OFM bot — Aiogram 3 + FastAPI (webhook)
# Rezyume WebApp + DOCX/PDF + Konvertatsiya + OCR + Tarjima
# Patchlar: pypdf overlay, taklif tugmasi ↔ sessiya urishmasin,
#           PENDING -> sessiyaga auto-ulanish, admin panel.
# -----------------------------------------------------------

import os
import io
import re
import json
import sys
import shutil
import tempfile
import traceback
import subprocess
from datetime import datetime
from typing import Optional, List, Tuple

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, Update,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile,
    FSInputFile
)

# --- PDF / DOCX / IMG / OCR helpers
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

from PIL import Image

from pdf2image import convert_from_path
from pypdf import PdfReader, PdfWriter  # <- pypdf: barqaror overlay

# --- Tarjima
from googletrans import Translator

# =========================
# KONFIG (env shart emas)
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"  # trailing slashsiz
GROUP_CHAT_ID = -1003046464831

# =========================
# GLOBAL STATE (RAM)
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

BASE_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
TMP_ROOT = "/tmp/ofm_bot"

os.makedirs(TMP_ROOT, exist_ok=True)

SESS: dict[int, dict] = {}       # {uid: {"op": str, "files":[{"path","name"}], "params":{...}}}
PENDING: dict[int, List[dict]] = {}  # sessiya yo‘q bo‘lsa, kelgan fayllarni vaqtincha ushlab turamiz
COUNTS = {
    "start": 0, "resume": 0,
    "convert": 0, "merge": 0, "split": 0,
    "ocr": 0, "translate": 0,
    "pagenum": 0, "watermark": 0,
}

translator = Translator()


# =========================
# UI: Klaviaturalar
# =========================
# === PATCH: klaviaturalar ===

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [
                KeyboardButton(text="🆕 Rezyume"),
                KeyboardButton(text="🔄 Konvert"),
                KeyboardButton(text="📎 Birlashtirish"),
            ],
            [
                KeyboardButton(text="✂️ Ajratish"),
                KeyboardButton(text="🔢 Raqamlash"),
                KeyboardButton(text="💧 Watermark"),
            ],
            [
                KeyboardButton(text="🔎 OCR"),
                KeyboardButton(text="🌐 Tarjima"),
            ],
            [
                KeyboardButton(text="ℹ️ Yordam"),
            ],
        ],
    )

def kb_session(op: str) -> ReplyKeyboardMarkup:
    suffix = {
        "convert": "🔄 Konvert",
        "merge": "📎 Birlashtirish",
        "split": "✂️ Ajratish",
        "pagenum": "🔢 Raqamlash",
        "watermark": "💧 Watermark",
        "ocr": "🔎 OCR",
        "translate": "🌐 Tarjima",
    }.get(op, "Jarayon")

    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")],
            [KeyboardButton(text="📋 Holat")],
            [KeyboardButton(text=f"↩️ Asosiy menyu ({suffix})")],
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




def kb_suggest() -> InlineKeyboardMarkup:
    # Fayl kelganda sessiya yo'q bo'lsa chiqadi
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Rasmni PDFga", callback_data="sug_to_pdf")],
        [InlineKeyboardButton(text="🔎 OCR", callback_data="sug_ocr")],
        [InlineKeyboardButton(text="🌐 Tarjima", callback_data="sug_tr")]
    ])

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
# UTIL
# =========================
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def user_dir(uid: int) -> str:
    p = os.path.join(TMP_ROOT, str(uid))
    ensure_dir(p)
    return p

def ext_of(name: str) -> str:
    return (os.path.splitext(name or "")[1] or "").lower()

def make_safe_name(s: str, default: str = "user") -> str:
    s = (s or "").strip()
    if not s:
        return default
    s = "_".join(s.split())
    return re.sub(r"[^A-Za-z0-9_]+", "", s) or default

def unique_name(base: str, ext: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return f"{base}_{ts}{ext}"

def save_bytes(path: str, data: bytes):
    ensure_dir(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(data)

def load_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except:
        return None

def human_op_name(op: str) -> str:
    return {
        "convert": "Konvert",
        "merge": "PDF birlashtirish",
        "split": "PDF ajratish",
        "pagenum": "Sahifa raqamlash",
        "watermark": "Watermark",
        "ocr": "OCR",
        "translate": "Tarjima",
    }.get(op, op)

# Session helpers
def new_session(uid: int, op: str):
    # TMP papkani tozalamaslik – PENDING fayllar yo‘qolmasin
    SESS[uid] = {"op": op, "files": [], "params": {}}

def get_session(uid: int) -> Optional[dict]:
    return SESS.get(uid)

def drop_session(uid: int):
    SESS.pop(uid, None)

def add_pending(uid: int, fdict: dict):
    PENDING.setdefault(uid, []).append(fdict)

def pop_pending(uid: int) -> List[dict]:
    arr = PENDING.get(uid) or []
    PENDING[uid] = []
    return arr

def session_status_text(uid: int) -> str:
    s = get_session(uid)
    if not s:
        return "❌ Sessiya yo‘q."
    lines = [f"🧭 Jarayon: {human_op_name(s['op'])}"]
    if s["files"]:
        lines.append(f"📎 Fayllar: {len(s['files'])}")
        for i, it in enumerate(s["files"], 1):
            lines.append(f"  {i}) {os.path.basename(it['path'])} ({it.get('size','')})")
    else:
        lines.append("📎 Fayl hali yuborilmadi")
    if s["params"]:
        lines.append(f"⚙️ Parametrlar: {s['params']}")
    else:
        lines.append("⚙️ Parametrlar hali berilmagan")
    lines.append("Yakunlash: ✅ Yakunlash | Bekor: ❌ Bekor")
    return "\n".join(lines)

# =========================
# DOCX <-> PDF
# =========================
def soffice_to_pdf(input_path: str) -> Optional[bytes]:
    """LibreOffice orqali * -> PDF"""
    try:
        with tempfile.TemporaryDirectory() as td:
            cmd = ["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, input_path]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            base = os.path.splitext(os.path.basename(input_path))[0]
            out_path = os.path.join(td, base + ".pdf")
            return load_bytes(out_path)
    except Exception as e:
        print("SOFFICE->PDF ERROR:", repr(e), file=sys.stderr)
        return None

def docx_render_from_template(tpl_path: str, ctx: dict, img_bytes: Optional[bytes]) -> Optional[bytes]:
    try:
        doc = DocxTemplate(tpl_path)
        if img_bytes:
            try:
                inline_img = InlineImage(doc, io.BytesIO(img_bytes), width=Mm(35))
            except Exception:
                inline_img = None
        else:
            inline_img = None
        ctx = dict(ctx)
        ctx["photo"] = inline_img
        buf = io.BytesIO()
        doc.render(ctx)
        doc.save(buf)
        return buf.getvalue()
    except Exception as e:
        print("DOCX RENDER ERROR:", repr(e), file=sys.stderr)
        return None

# =========================
# PDF: overlay & pagenum (pypdf)
# =========================
def pdf_overlay_text(pdf_path: str, text: str, pos: str = "bottom-center") -> Optional[bytes]:
    try:
        base = PdfReader(pdf_path)
        out = PdfWriter()
        total = len(base.pages)
        for idx in range(total):
            page = base.pages[idx]
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)

            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFont("Helvetica", 14)
            if pos == "bottom-center":
                c.drawCentredString(w/2, 12*mm, text)
            elif pos == "top-right":
                c.drawRightString(w-12*mm, h-12*mm, text)
            else:
                c.drawCentredString(w/2, 12*mm, text)
            c.save()
            buf.seek(0)

            overlay = PdfReader(buf).pages[0]
            page.merge_page(overlay)
            out.add_page(page)

        bio = io.BytesIO()
        out.write(bio)
        return bio.getvalue()
    except Exception as e:
        print("PDF OVERLAY ERROR:", repr(e), file=sys.stderr)
        return None

def pdf_add_pagenumbers(pdf_path: str, pos: str = "bottom-center") -> Optional[bytes]:
    try:
        base = PdfReader(pdf_path)
        out = PdfWriter()
        total = len(base.pages)
        for i, page in enumerate(base.pages, start=1):
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)

            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFont("Helvetica", 12)
            label = f"{i} / {total}"
            if pos == "bottom-center":
                c.drawCentredString(w/2, 10*mm, label)
            elif pos == "top-right":
                c.drawRightString(w-10*mm, h-10*mm, label)
            else:
                c.drawCentredString(w/2, 10*mm, label)
            c.save()
            buf.seek(0)

            overlay = PdfReader(buf).pages[0]
            page.merge_page(overlay)
            out.add_page(page)

        bio = io.BytesIO()
        out.write(bio)
        return bio.getvalue()
    except Exception as e:
        print("PAGENUM ERROR:", repr(e), file=sys.stderr)
        return None

# =========================
# OCR & Tarjima
# =========================
def ocr_image_bytes(img: Image.Image, lang_hint: Optional[str] = None) -> str:
    import pytesseract
    cfg = "--psm 6"
    if lang_hint == "auto" or not lang_hint:
        # Mavjud bo'lsa ko'p tilli; bo'lmasa 'eng'ga fallback
        try:
            return pytesseract.image_to_string(img, lang="uzb+rus+eng", config=cfg)
        except Exception:
            return pytesseract.image_to_string(img, lang="eng", config=cfg)
    else:
        try:
            return pytesseract.image_to_string(img, lang=lang_hint, config=cfg)
        except Exception:
            return pytesseract.image_to_string(img, lang="eng", config=cfg)

def images_to_pdf(image_paths: List[str]) -> Optional[bytes]:
    try:
        pil_imgs = []
        for p in image_paths:
            if not os.path.exists(p):
                continue
            im = Image.open(p).convert("RGB")
            pil_imgs.append(im)
        if not pil_imgs:
            return None
        bio = io.BytesIO()
        first, rest = pil_imgs[0], pil_imgs[1:]
        first.save(bio, format="PDF", save_all=True, append_images=rest)
        return bio.getvalue()
    except Exception as e:
        print("IMG->PDF ERROR:", repr(e), file=sys.stderr)
        return None

def pdf_to_images(pdf_path: str) -> List[str]:
    try:
        pages = convert_from_path(pdf_path)
        outs = []
        for i, im in enumerate(pages, 1):
            td = os.path.dirname(pdf_path)
            fn = os.path.splitext(os.path.basename(pdf_path))[0]
            out = os.path.join(td, f"{fn}_p{i:03d}.png")
            im.save(out, "PNG")
            outs.append(out)
        return outs
    except Exception as e:
        print("PDF->IMAGES ERROR:", repr(e), file=sys.stderr)
        return []

# =========================
# FASTAPI APP
# =========================
app = FastAPI()

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print("=== GLOBAL ERROR ===", file=sys.stderr)
    print(repr(exc), file=sys.stderr)
    traceback.print_exc()
    # 200 qaytaramiz (WebApp'ga qulay)
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/admin", response_class=HTMLResponse)
def admin():
    # Juda yengil Bootstrap dashboard
    html = f"""
    <html><head>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
    <title>OFM Admin</title>
    </head><body class="p-4">
    <h3>📊 OFM — Statistikalar (RAM)</h3>
    <table class="table table-sm table-bordered w-auto">
      <tr><th>Start</th><td>{COUNTS['start']}</td></tr>
      <tr><th>Rezyume</th><td>{COUNTS['resume']}</td></tr>
      <tr><th>Konvert</th><td>{COUNTS['convert']}</td></tr>
      <tr><th>Birlashtirish</th><td>{COUNTS['merge']}</td></tr>
      <tr><th>Ajratish</th><td>{COUNTS['split']}</td></tr>
      <tr><th>OCR</th><td>{COUNTS['ocr']}</td></tr>
      <tr><th>Tarjima</th><td>{COUNTS['translate']}</td></tr>
      <tr><th>Raqamlash</th><td>{COUNTS['pagenum']}</td></tr>
      <tr><th>Watermark</th><td>{COUNTS['watermark']}</td></tr>
    </table>
    <p class="text-muted">Eslatma: bu hisoblagichlar RAM’da. Deploy qayta ishga tushsa, nolga tushadi.</p>
    </body></html>
    """
    return html

# --- WebApp: forma
@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

@app.post("/send_resume_data")
async def send_resume_data(
    # Barchasi ixtiyoriy — 422 bo‘lmasin
    full_name: str | None = Form(None),
    phone: str | None = Form(None),
    tg_id: str | None = Form(None),
    birth_date: str | None = Form(None),
    birth_place: str | None = Form(None),
    nationality: str | None = Form("O‘zbek"),
    party_membership: str | None = Form("Yo‘q"),
    education: str | None = Form(None),
    university: str | None = Form(None),
    specialization: str | None = Form("Yo‘q"),
    ilmiy_daraja: str | None = Form("Yo‘q"),
    ilmiy_unvon: str | None = Form("Yo‘q"),
    languages: str | None = Form("Yo‘q"),
    dav_mukofoti: str | None = Form("Yo‘q"),
    deputat: str | None = Form("Yo‘q"),
    adresss: str | None = Form(None),
    current_position_date: str | None = Form(None),
    current_position_full: str | None = Form(None),
    work_experience: str | None = Form(None),
    relatives: str | None = Form("[]"),
    photo: UploadFile | None = None,
):
    # Safe defaults
    full_name = full_name or "—"
    phone = phone or "—"
    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

    ctx = {
        "full_name": full_name,
        "phone": phone,
        "birth_date": birth_date or "—",
        "birth_place": birth_place or "—",
        "nationality": nationality or "—",
        "party_membership": party_membership or "—",
        "education": education or "—",
        "university": university or "—",
        "specialization": specialization or "—",
        "ilmiy_daraja": ilmiy_daraja or "—",
        "ilmiy_unvon": ilmiy_unvon or "—",
        "languages": languages or "—",
        "dav_mukofoti": dav_mukofoti or "—",
        "deputat": deputat or "—",
        "adresss": adresss or "—",
        "current_position_date": current_position_date or "—",
        "current_position_full": current_position_full or "—",
        "work_experience": work_experience or "—",
        "relatives": rels,
    }

    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return {"status": "error", "error": "resume.docx topilmadi"}

    img_bytes = None
    if photo and getattr(photo, "filename", ""):
        try:
            img_bytes = await photo.read()
        except:
            img_bytes = None

    # DOCX
    docx_bytes = docx_render_from_template(tpl_path, ctx, img_bytes)
    if not docx_bytes:
        return {"status": "error", "error": "DOCX render xato"}

    # PDF
    with tempfile.TemporaryDirectory() as td:
        docx_path = os.path.join(td, "resume.docx")
        save_bytes(docx_path, docx_bytes)
        pdf_bytes = soffice_to_pdf(docx_path)

    # Nomlash
    base = make_safe_name(full_name)
    docx_name = f"{base}_0.docx"
    pdf_name = f"{base}_0.pdf"

    # Guruhga: rasm + json
    try:
        payload = dict(ctx)
        payload["timestamp"] = datetime.utcnow().isoformat() + "Z"
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        if img_bytes:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img_bytes, filename=f"{base}.png"),
                caption=f"🆕 Rezyume: {full_name}\n📞 {phone}"
            )
        await bot.send_document(
            GROUP_CHAT_ID,
            BufferedInputFile(json_bytes, filename=f"{base}.json"),
            caption=f"📄 Ma'lumotlar JSON: {full_name}"
        )
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)

    # Foydalanuvchiga yuborish (tg_id bo‘lsa)
    try:
        if tg_id and str(tg_id).isdigit():
            cid = int(tg_id)
            await bot.send_document(cid, BufferedInputFile(docx_bytes, filename=docx_name),
                                    caption="✅ Word formatdagi rezyume")
            if pdf_bytes:
                await bot.send_document(cid, BufferedInputFile(pdf_bytes, filename=pdf_name),
                                        caption="✅ PDF formatdagi rezyume")
            else:
                await bot.send_message(cid, "⚠️ PDF konvertda xato, hozircha faqat Word yuborildi.")
    except Exception as e:
        print("USER SEND ERROR:", repr(e), file=sys.stderr)

    COUNTS["resume"] += 1
    return {"status": "success"}

# =========================
# BOT HANDLERS
# =========================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    COUNTS["start"] += 1
    await m.answer(
        "👋 Assalomu alaykum!\n"
        "Bu bot fayllar bilan tezkor ishlashga yordam beradi.\n"
        "Quyidagi menyudan keraklisini tanlang.",
        reply_markup=kb_main()
    )

@dp.message(F.text.in_({"ℹ️ Yordam", "/help"}))
async def cmd_help(m: Message):
    await m.answer(
        "Qo‘llanma:\n"
        "• 🆕 Rezyume — WebApp orqali ma'lumot kiriting, bot DOCX+PDF yuboradi.\n"
        "• 🔄 Konvert — bitta fayl -> kerakli formatga.\n"
        "• 📎 Birlashtirish — bir nechta PDF -> bitta PDF.\n"
        "• ✂️ Ajratish — PDF sahifa oralig‘ini ajratib oling (masalan 1-3,5).\n"
        "• 🔢 Raqamlash — PDF sahifalar pastida raqam.\n"
        "• 💧 Watermark — PDF sahifalarga matn qo‘shish.\n"
        "• 🔎 OCR — rasm/PDFdan matn chiqarish.\n"
        "• 🌐 Tarjima — matn/rasm/PDF (OCR orqali) tarjimasi.\n"
        "Yakun: ✅ Yakunlash, Bekor: ❌ Bekor, Holat: 📋 Holat.",
        reply_markup=kb_main()
    )

# --- Rezyume WebApp shortcut
@dp.message(F.text.in_({"🆕 Rezyume", "/new_resume"}))
async def new_resume(m: Message):
    await m.answer(
        "👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
        "✅ Tez\n✅ Oson\n✅ Ishonchli\n"
        "quyidagi 🌐 web formani to'ldiring\n👇👇👇👇👇👇👇👇👇",
        reply_markup=None
    )
    await m.answer(" ", reply_markup=kb_webapp(m.from_user.id))

# ---------- Session openers ----------
async def _open_session(m: Message, op: str, intro: str):
    uid = m.from_user.id
    new_session(uid, op)
    # PENDING fayllarni auto-ulash
    pend = pop_pending(uid)
    if pend:
        SESS[uid]["files"] = [x for x in pend if os.path.exists(x["path"])]
    await m.answer(intro, reply_markup=kb_session(op))
    await m.answer(session_status_text(uid), reply_markup=kb_session(op))

@dp.message(F.text.in_({"🔄 Konvert", "/convert"}))
async def open_convert(m: Message):
    await _open_session(m, "convert",
        "🔄 Konvert sessiyasi boshlandi.\n1) Bitta fayl yuboring (DOCX/PPTX/XLSX/PDF yoki rasm).\n"
        "2) Maqsad: /target pdf|png|jpg|docx|pptx\n"
        "Yakun: ✅ Yakunlash | Bekor: ❌ Bekor | Holat: 📋 Holat"
    )

@dp.message(F.text.in_({"📎 Birlashtirish", "/pdf_merge"}))
async def open_merge(m: Message):
    await _open_session(m, "merge",
        "📎 PDF birlashtirish boshlandi.\nBir nechta PDF yuboring, so'ng ✅ Yakunlash bosing."
    )

@dp.message(F.text.in_({"✂️ Ajratish", "/pdf_split"}))
async def open_split(m: Message):
    await _open_session(m, "split",
        "✂️ PDF ajratish boshlandi.\nPDF yuboring, so'ng /range 1-3,5 shaklida interval bering."
    )

@dp.message(F.text.in_({"🔢 Raqamlash", "/pagenum"}))
async def open_pagenum(m: Message):
    await _open_session(m, "pagenum",
        "🔢 Sahifa raqamlash boshlandi.\nPDF yuboring, so'ng ✅ Yakunlash bosing."
    )

@dp.message(F.text.in_({"💧 Watermark", "/watermark"}))
async def open_watermark(m: Message):
    await _open_session(m, "watermark",
        "💧 Watermark sessiyasi boshlandi.\nPDF yuboring, so'ng /text <matn> yuboring."
    )

@dp.message(F.text.in_({"🔎 OCR", "/ocr"}))
async def open_ocr(m: Message):
    await _open_session(m, "ocr",
        "🔎 OCR sessiyasi boshlandi.\nRasm/PDF yuboring, so'ng ✅ Yakunlash bosing."
    )

@dp.message(F.text.in_({"🌐 Tarjima", "/translate"}))
async def open_translate(m: Message):
    await _open_session(m, "translate",
        "🌐 Tarjima sessiyasi boshlandi.\nRasm/PDF yuboring (OCR orqali), yoki matn yuboring.\n"
        "Maqsad til: /tgt en|ru|uz|tr ... (default: uz)."
    )

# ---------- Parametrlar ----------
@dp.message(F.text.regexp(r"^/target(\s+)(pdf|png|jpg|docx|pptx)$"))
async def set_target(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    if not s or s["op"] != "convert":
        return await m.answer("⚠️ Avval 🔄 Konvert sessiyasini oching.", reply_markup=kb_main())
    tgt = m.text.split()[-1].lower()
    s["params"]["target"] = tgt
    await m.answer(f"🎯 Target: {tgt}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.regexp(r"^/range\s+[\d,\-\s]+$"))
async def set_range(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    if not s or s["op"] != "split":
        return await m.answer("⚠️ Avval ✂️ Ajratish sessiyasini oching.", reply_markup=kb_main())
    s["params"]["range"] = m.text.replace("/range", "").strip()
    await m.answer(f"📐 Range: {s['params']['range']}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.regexp(r"^/text\s+.+"))
async def set_text(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    if not s or s["op"] != "watermark":
        return await m.answer("⚠️ Avval 💧 Watermark sessiyasini oching.", reply_markup=kb_main())
    s["params"]["text"] = m.text.replace("/text", "", 1).strip()
    await m.answer(f"📝 Watermark matn: {s['params']['text']}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.regexp(r"^/tgt\s+[a-z]{2,5}$"))
async def set_tgt(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    if not s or s["op"] != "translate":
        return await m.answer("⚠️ Avval 🌐 Tarjima sessiyasini oching.", reply_markup=kb_main())
    s["params"]["tgt"] = m.text.split()[-1].lower()
    await m.answer(f"🎯 Tarjima tili: {s['params']['tgt']}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.in_({"📋 Holat", "/status"}))
async def show_status(m: Message):
    await m.answer(session_status_text(m.from_user.id), reply_markup=kb_session(get_session(m.from_user.id)["op"]) if get_session(m.from_user.id) else kb_main())

@dp.message(F.text.in_({"❌ Bekor", "/cancel"}))
async def cancel_session(m: Message):
    drop_session(m.from_user.id)
    await m.answer("❌ Sessiya bekor qilindi.", reply_markup=kb_main())

# ---------- Fayl qabul (document/photo) ----------
async def save_tg_document(m: Message) -> Optional[Tuple[str, str]]:
    """Dokumentni /tmp ga saqlash; (path, name) qaytaradi."""
    try:
        uid = m.from_user.id
        ud = user_dir(uid)
        doc = m.document
        fname = doc.file_name or f"file_{doc.file_unique_id}"
        path = os.path.join(ud, unique_name(os.path.splitext(fname)[0], ext_of(fname)))
        file = await bot.get_file(doc.file_id)
        bio = await bot.download_file(file.file_path)
        save_bytes(path, bio.read())
        return path, os.path.basename(path)
    except Exception as e:
        print("SAVE DOC ERROR:", repr(e), file=sys.stderr)
        return None

async def save_tg_photo(m: Message) -> Optional[Tuple[str, str]]:
    try:
        uid = m.from_user.id
        ud = user_dir(uid)
        ph = m.photo[-1]  # eng kattasi
        path = os.path.join(ud, unique_name("photo", ".jpg"))
        file = await bot.get_file(ph.file_id)
        bio = await bot.download_file(file.file_path)
        save_bytes(path, bio.read())
        return path, os.path.basename(path)
    except Exception as e:
        print("SAVE PHOTO ERROR:", repr(e), file=sys.stderr)
        return None

@dp.message(F.document)
async def on_document(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    saved = await save_tg_document(m)
    if not saved:
        return await m.answer("❌ Faylni saqlab bo‘lmadi.")
    path, name = saved
    if s:
        s["files"].append({"path": path, "name": name, "size": f"{m.document.file_size or ''}B"})
        return await m.answer("📎 Fayl qo‘shildi.", reply_markup=kb_session(s["op"]))
    # sessiya yo‘q — taklif
    add_pending(uid, {"path": path, "name": name})
    await m.answer(
        f"📑 Fayl qabul qilindi: {name}\nQuyidagilardan birini tanlang:",
        reply_markup=kb_suggest()
    )

@dp.message(F.photo)
async def on_photo(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    saved = await save_tg_photo(m)
    if not saved:
        return await m.answer("❌ Rasmni saqlab bo‘lmadi.")
    path, name = saved
    if s:
        s["files"].append({"path": path, "name": name})
        return await m.answer("🖼️ Rasm qo‘shildi.", reply_markup=kb_session(s["op"]))
    add_pending(uid, {"path": path, "name": name})
    await m.answer(
        f"🖼️ Fayl qabul qilindi: {name}\nQuyidagilardan birini tanlang:",
        reply_markup=kb_suggest()
    )

# --- Taklif tugmalari (sessiya yo'q bo'lsa)
@dp.callback_query(F.data.in_({"sug_to_pdf", "sug_ocr", "sug_tr"}))
async def cb_suggest(c: CallbackQuery):
    uid = c.from_user.id
    if get_session(uid):
        return await c.message.answer("⚠️ Avval joriy sessiyani yakunlang yoki ❌ Bekor qiling.", reply_markup=kb_session(get_session(uid)["op"]))
    if not (PENDING.get(uid)):
        return await c.message.answer("⚠️ Mos fayl yo‘q.")
    data = c.data
    if data == "sug_to_pdf":
        await open_convert(c.message)
        SESS[uid]["params"]["target"] = "pdf"
    elif data == "sug_ocr":
        await open_ocr(c.message)
    elif data == "sug_tr":
        await open_translate(c.message)
        SESS[uid]["params"]["tgt"] = "uz"
    await c.answer()

# ---------- DONE (ish bajarish) ----------
@dp.message(F.text.in_({"✅ Yakunlash", "/done"}))
async def do_done(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    if not s:
        return await m.answer("❌ Sessiya yo‘q.", reply_markup=kb_main())

    # fayllar mavjudligini tekshirish
    s["files"] = [f for f in s["files"] if f.get("path") and os.path.exists(f["path"])]
    if not s["files"]:
        return await m.answer("PDF yig‘ish uchun mos fayl yo‘q.", reply_markup=kb_session(s["op"]))

    op = s["op"]
    try:
        if op == "merge":
            # Merge PDFs
            writer = PdfWriter()
            for it in s["files"]:
                if ext_of(it["name"]) != ".pdf":
                    continue
                reader = PdfReader(it["path"])
                for p in reader.pages:
                    writer.add_page(p)
            bio = io.BytesIO(); writer.write(bio)
            out = bio.getvalue()
            await bot.send_document(uid, BufferedInputFile(out, filename="merged.pdf"))
            COUNTS["merge"] += 1

        elif op == "split":
            rng = s["params"].get("range")
            if not rng:
                return await m.answer("⚠️ Avval /range 1-3,5 ko‘rinishida kiriting.", reply_markup=kb_session(op))
            # bitta pdf kutiladi
            main = [it for it in s["files"] if ext_of(it["name"]) == ".pdf"]
            if not main:
                return await m.answer("⚠️ PDF yuboring.", reply_markup=kb_session(op))
            reader = PdfReader(main[0]["path"])
            pages = []
            for token in rng.replace(" ", "").split(","):
                if "-" in token:
                    a, b = token.split("-", 1)
                    a, b = int(a), int(b)
                    pages.extend(list(range(a, b+1)))
                else:
                    pages.append(int(token))
            pages = [p for p in pages if 1 <= p <= len(reader.pages)]
            writer = PdfWriter()
            for p in pages:
                writer.add_page(reader.pages[p-1])
            bio = io.BytesIO(); writer.write(bio)
            await bot.send_document(uid, BufferedInputFile(bio.getvalue(), filename="split.pdf"))
            COUNTS["split"] += 1

        elif op == "pagenum":
            pdfs = [it for it in s["files"] if ext_of(it["name"]) == ".pdf"]
            if not pdfs:
                return await m.answer("⚠️ PDF yuboring.", reply_markup=kb_session(op))
            out = pdf_add_pagenumbers(pdfs[0]["path"])
            if not out:
                return await m.answer("❌ Raqamlashda xato.", reply_markup=kb_session(op))
            await bot.send_document(uid, BufferedInputFile(out, filename="pagenum.pdf"))
            COUNTS["pagenum"] += 1

        elif op == "watermark":
            txt = s["params"].get("text")
            if not txt:
                return await m.answer("⚠️ Avval /text <matn> yuboring.", reply_markup=kb_session(op))
            pdfs = [it for it in s["files"] if ext_of(it["name"]) == ".pdf"]
            if not pdfs:
                return await m.answer("⚠️ PDF yuboring.", reply_markup=kb_session(op))
            out = pdf_overlay_text(pdfs[0]["path"], txt)
            if not out:
                return await m.answer("❌ Watermark xatosi.", reply_markup=kb_session(op))
            await bot.send_document(uid, BufferedInputFile(out, filename="watermark.pdf"))
            COUNTS["watermark"] += 1

        elif op == "ocr":
            # PDF bo'lsa rasmlarga ajratamiz, rasm bo'lsa darrov
            texts: List[str] = []
            imgs: List[Image.Image] = []

            pdfs = [it for it in s["files"] if ext_of(it["name"]) == ".pdf"]
            if pdfs:
                img_paths = pdf_to_images(pdfs[0]["path"])
                for p in img_paths:
                    try:
                        imgs.append(Image.open(p))
                    except:
                        pass
            else:
                for it in s["files"]:
                    if ext_of(it["name"]) in {".png", ".jpg", ".jpeg", ".webp"}:
                        try:
                            imgs.append(Image.open(it["path"]))
                        except:
                            pass
            if not imgs:
                return await m.answer("⚠️ OCR uchun rasm/PDF kerak.", reply_markup=kb_session(op))

            for im in imgs:
                texts.append(ocr_image_bytes(im, "auto"))
            text_out = "\n\n".join(texts).strip() or "(matn topilmadi)"
            await bot.send_document(uid, BufferedInputFile(text_out.encode("utf-8"), filename="ocr.txt"))
            COUNTS["ocr"] += 1

        elif op == "translate":
            tgt = s["params"].get("tgt", "uz")
            # Agar fayl bo'lsa: OCR->tarjima; matn bo'lsa: to'g'ridan
            if s["files"]:
                # OCR qilamiz
                imgs: List[Image.Image] = []
                pdfs = [it for it in s["files"] if ext_of(it["name"]) == ".pdf"]
                if pdfs:
                    for p in pdf_to_images(pdfs[0]["path"]):
                        try: imgs.append(Image.open(p))
                        except: pass
                else:
                    for it in s["files"]:
                        if ext_of(it["name"]) in {".png", ".jpg", ".jpeg", ".webp"}:
                            try: imgs.append(Image.open(it["path"]))
                            except: pass
                if not imgs:
                    return await m.answer("⚠️ Tarjima uchun rasm/PDF yuboring yoki matn yozing.", reply_markup=kb_session(op))
                ocr_text = "\n\n".join(ocr_image_bytes(im, "auto") for im in imgs).strip()
                if not ocr_text:
                    return await m.answer("⚠️ OCR natijasi bo‘sh.", reply_markup=kb_session(op))
                tr = translator.translate(ocr_text, dest=tgt)
                await bot.send_document(uid, BufferedInputFile(tr.text.encode("utf-8"), filename=f"translate_{tgt}.txt"))
            COUNTS["translate"] += 1

        elif op == "convert":
            tgt = s["params"].get("target")
            if not tgt:
                return await m.answer("⚠️ Avval /target pdf|png|jpg|docx|pptx belgilang.", reply_markup=kb_session(op))
            it = s["files"][0]  # bitta fayl
            ext = ext_of(it["name"])
            out_bytes = None
            out_name = f"convert.{tgt}"

            if tgt == "pdf":
                if ext in {".png", ".jpg", ".jpeg", ".webp"}:
                    out_bytes = images_to_pdf([it["path"]])
                else:
                    out_bytes = soffice_to_pdf(it["path"])
            elif tgt in {"png", "jpg"} and ext == ".pdf":
                imgs = pdf_to_images(it["path"])
                if not imgs:
                    return await m.answer("❌ PDF->rasm xatosi.", reply_markup=kb_session(op))
                for p in imgs[:8]:  # juda ko‘p bo‘lsa spam bo‘lmasin
                    await bot.send_document(uid, FSInputFile(p))
                out_bytes = None
            elif tgt == "docx" and ext in {".pdf", ".doc", ".odt", ".rtf"}:
                # PDF->DOCX bevosita sifatli emas; LibreOffice orqali .docxga
                with tempfile.TemporaryDirectory() as td:
                    tmp = os.path.join(td, os.path.basename(it["path"]))
                    shutil.copy(it["path"], tmp)
                    # LibreOffice export to docx
                    cmd = ["soffice", "--headless", "--convert-to", "docx", "--outdir", td, tmp]
                    subprocess.run(cmd, check=True)
                    outp = os.path.join(td, os.path.splitext(os.path.basename(it["path"]))[0] + ".docx")
                    if os.path.exists(outp):
                        out_bytes = load_bytes(outp)
                        out_name = os.path.basename(outp)
            elif tgt == "pptx" and ext in {".pdf", ".ppt"}:
                with tempfile.TemporaryDirectory() as td:
                    tmp = os.path.join(td, os.path.basename(it["path"]))
                    shutil.copy(it["path"], tmp)
                    cmd = ["soffice", "--headless", "--convert-to", "pptx", "--outdir", td, tmp]
                    subprocess.run(cmd, check=True)
                    outp = os.path.join(td, os.path.splitext(os.path.basename(it["path"]))[0] + ".pptx")
                    if os.path.exists(outp):
                        out_bytes = load_bytes(outp)
                        out_name = os.path.basename(outp)

            if out_bytes:
                await bot.send_document(uid, BufferedInputFile(out_bytes, filename=out_name))
            await m.answer("✅ Yakunlandi.", reply_markup=kb_main())
            COUNTS["convert"] += 1
            drop_session(uid)
            return

        # umumiy yakun
        await m.answer("✅ Yakunlandi.", reply_markup=kb_main())
        drop_session(uid)

    except Exception as e:
        print("DONE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        await m.answer("❌ Xatolik yuz berdi.", reply_markup=kb_session(op))

# ---------- Fallback: matn yuborilganda translate sessiyasida qabul qilish ----------
@dp.message(F.text)
async def on_text(m: Message):
    t = m.text.strip()
    uid = m.from_user.id
    # Asosiy menyu "↩️ Asosiy menyu (...)" tugmasi
    if t.startswith("↩️ Asosiy menyu"):
        drop_session(uid)
        return await m.answer("Menyu:", reply_markup=kb_main())

    s = get_session(uid)
    if s and s["op"] == "translate" and t and not t.startswith("/"):
        tgt = s["params"].get("tgt", "uz")
        tr = translator.translate(t, dest=tgt)
        await m.answer(f"🔁 {tgt}:\n{tr.text}", reply_markup=kb_session("translate"))
        return

    # boshqalar — foydali maslahat
    if not s:
        await m.answer("Kerakli bo‘limni tanlang 👇", reply_markup=kb_main())
    else:
        await m.answer(session_status_text(uid), reply_markup=kb_session(s["op"]))

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
