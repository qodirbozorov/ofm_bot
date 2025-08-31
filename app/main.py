# app/main.py
import os
import io
import re
import json
import sys
import subprocess
import tempfile
import traceback
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Update,
    BufferedInputFile, BotCommand
)

# =========================
# KONFIG (env shart emas)
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"  # trailing slashsiz
GROUP_CHAT_ID = -1003046464831  # ma'lumot jo'natiladigan guruh

# =========================
# AIROGRAM
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ACTIVE_USERS = set()

async def set_commands():
    commands = [
        BotCommand(command="start",       description="Boshlash"),
        BotCommand(command="help",        description="Yordam"),
        BotCommand(command="new_resume",  description="Yangi obyektivka"),

        BotCommand(command="convert",     description="DOCX/PPTX/XLSX↔PDF, PPTX→PNG, PDF→DOCX/PPTX"),
        BotCommand(command="pdf_split",   description="PDF sahifalarni ajratish (caption)"),
        BotCommand(command="pdf_merge",   description="PDF fayllarni birlashtirish (sessiya)"),
        BotCommand(command="pagenum",     description="PDF sahifalarga raqam qo‘shish (caption)"),
        BotCommand(command="watermark",   description="PDF watermark (caption)"),
        BotCommand(command="ocr",         description="Skan PDF → matn (caption)"),
        BotCommand(command="translate",   description="PDF matn tarjimasi (caption)"),
        BotCommand(command="done",        description="Merge sessiyasini yakunlash"),
    ]
    await bot.set_my_commands(commands)

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
    base = (APP_BASE or "").rstrip("/")
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
# FASTAPI
# =========================
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    try:
        await set_commands()
        print("✅ Bot commands list yangilandi", file=sys.stderr)
    except Exception as e:
        print("❌ Commands set xato:", e, file=sys.stderr)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print("=== GLOBAL ERROR ===", file=sys.stderr)
    print(repr(exc), file=sys.stderr)
    traceback.print_exc()
    # WebApp alert uchun 200 bilan JSON qaytaramiz
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)

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
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# =========================
# YORDAMCHI: nomlash va rasm ext
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

# =========================
# LibreOffice konvert
# =========================
def soffice_convert(src_bytes: bytes, in_ext: str, out_ext: str) -> Optional[bytes]:
    """
    LO orqali umumiy konvert:
      - * -> pdf/docx/pptx/xlsx => bitta chiqish fayl
      - pptx -> png => ko'p fayl (zip qilib qaytaramiz)
    """
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, f"in{in_ext}")
        with open(inp, "wb") as f:
            f.write(src_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", out_ext, "--outdir", td, inp],
                check=True
            )
        except Exception as e:
            print("SOFFICE CONVERT ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None

        if out_ext in {"pdf", "docx", "pptx", "xlsx"}:
            out_path = os.path.join(td, f"in.{out_ext}")
            if not os.path.exists(out_path):
                # ba'zi LO versiyalarda nomi boshqacha bo‘lishi mumkin — ehtiyot chorasi:
                for name in os.listdir(td):
                    if name.lower().endswith(f".{out_ext}"):
                        out_path = os.path.join(td, name)
                        break
            if os.path.exists(out_path):
                return open(out_path, "rb").read()
            return None

        if out_ext == "png":
            files = sorted(
                os.path.join(td, x) for x in os.listdir(td) if x.lower().endswith(".png")
            )
            if not files:
                return None
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for i, p in enumerate(files, 1):
                    z.write(p, arcname=f"slide-{i}.png")
            return buf.getvalue()

        return None

def convert_docx_to_pdf(docx_bytes: bytes) -> Optional[bytes]:
    return soffice_convert(docx_bytes, in_ext=".docx", out_ext="pdf")

# =========================
# PDF OPS: split / merge / pagenum / watermark
# =========================
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

def _parse_ranges(spec: str):
    out = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.append((int(a), int(b)))
        else:
            n = int(part)
            out.append((n, n))
    return out

def pdf_split(src: bytes, range_spec: str) -> bytes:
    r = PdfReader(io.BytesIO(src))
    w = PdfWriter()
    total = len(r.pages)
    for a, b in _parse_ranges(range_spec):
        a = max(1, a); b = min(total, b)
        for i in range(a-1, b):
            w.add_page(r.pages[i])
    buf = io.BytesIO(); w.write(buf); return buf.getvalue()

def pdf_merge(parts: list[bytes]) -> bytes:
    w = PdfWriter()
    for data in parts:
        r = PdfReader(io.BytesIO(data))
        for p in r.pages:
            w.add_page(p)
    buf = io.BytesIO(); w.write(buf); return buf.getvalue()

def pdf_add_page_numbers(src: bytes, position: str="bottom-right") -> bytes:
    r = PdfReader(io.BytesIO(src))
    w = PdfWriter()
    total = len(r.pages)
    for idx in range(total):
        p = r.pages[idx]
        pw = float(p.mediabox.width); ph = float(p.mediabox.height)
        layer = io.BytesIO(); c = canvas.Canvas(layer, pagesize=(pw, ph))
        try:
            pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
            c.setFont("DejaVu", 10)
        except:
            c.setFont("Helvetica", 10)
        margin = 12*mm
        pos = {
            "bottom-right": (pw-margin, margin, True),
            "bottom-left":  (margin, margin, False),
            "top-right":    (pw-margin, ph-margin, True),
            "top-left":     (margin, ph-margin, False),
            "bottom-center":(pw/2, margin, False),
            "top-center":   (pw/2, ph-margin, False),
        }.get(position, (pw-margin, margin, True))
        x, y, align_right = pos
        if align_right:
            c.drawRightString(x, y, f"{idx+1}/{total}")
        else:
            c.drawString(x, y, f"{idx+1}/{total}")
        c.save(); layer.seek(0)
        from pypdf import PdfReader as _PR
        n = _PR(layer)
        p.merge_page(n.pages[0]); w.add_page(p)
    buf = io.BytesIO(); w.write(buf); return buf.getvalue()

def pdf_watermark(src: bytes, text: str) -> bytes:
    r = PdfReader(io.BytesIO(src)); w = PdfWriter()
    p0 = r.pages[0]; pw = float(p0.mediabox.width); ph = float(p0.mediabox.height)
    lay = io.BytesIO(); c = canvas.Canvas(lay, pagesize=(pw, ph))
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        c.setFont("DejaVu", 48)
    except:
        c.setFont("Helvetica", 48)
    c.saveState(); c.translate(pw/2, ph/2); c.rotate(45)
    c.setFillGray(0.2)
    c.drawCentredString(0, 0, text)
    c.restoreState(); c.save(); lay.seek(0)
    wm = PdfReader(lay)
    for i in range(len(r.pages)):
        page = r.pages[i]; page.merge_page(wm.pages[0]); w.add_page(page)
    buf = io.BytesIO(); w.write(buf); return buf.getvalue()

# =========================
# OCR va TARJIMA
# =========================
import pytesseract
from pdf2image import convert_from_bytes
import fitz  # PyMuPDF
from deep_translator import GoogleTranslator

def ocr_pdf_to_text(src: bytes, lang: str="eng") -> str:
    imgs = convert_from_bytes(src, dpi=220)
    outs = []
    for im in imgs:
        outs.append(pytesseract.image_to_string(im, lang=lang))
    return "\n\n".join(outs)

def extract_pdf_text(src: bytes) -> str:
    doc = fitz.open(stream=src, filetype="pdf")
    out = []
    for p in doc:
        out.append(p.get_text("text"))
    return "\n".join(out)

def translate_text(text: str, dest: str="uz", src_lang: str="auto") -> str:
    gt = GoogleTranslator(source=src_lang, target=dest)
    return gt.translate(text)

# =========================
# FORMA QABUL QILISH (DB yo‘q)
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

    # template tekshir
    tpl_path = os.path.join(TEMPLATES_DIR, "resume.docx")
    if not os.path.exists(tpl_path):
        return JSONResponse({"status": "error", "error": "resume.docx template topilmadi"}, status_code=200)

    # context
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

    # DOCX render + rasm (ixtiyoriy)
    doc = DocxTemplate(tpl_path)
    inline_img = None
    img = None  # guruhga file sifatida yuborish uchun
    img_ext = ".png"
    try:
        if photo is not None and getattr(photo, "filename", ""):
            img = await photo.read()
            img_ext = pick_image_ext(photo.filename)
            if img:
                inline_img = InlineImage(doc, io.BytesIO(img), width=Mm(35))
    except Exception as e:
        print("PHOTO ERROR:", repr(e), file=sys.stderr)

    ctx["photo"] = inline_img

    # DOCX bytes
    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()

    # PDF bytes
    pdf_bytes = convert_docx_to_pdf(docx_bytes)

    # nomlar
    base_name = make_safe_basename(full_name, phone)
    docx_name = f"{base_name}_0.docx"
    pdf_name  = f"{base_name}_0.pdf"
    img_name  = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # GURUHGA: rasm + json
    try:
        if img:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img, filename=img_name),
                caption=f"🆕 Yangi forma: {full_name}\n📞 {phone}\n👤 TG: {tg_id}"
            )
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "tg_id": tg_id,
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
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await bot.send_document(
            GROUP_CHAT_ID,
            BufferedInputFile(json_bytes, filename=json_name),
            caption=f"📄 Ma'lumotlar JSON: {full_name}"
        )
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()

    # MIJOZGA: DOCX + PDF
    try:
        chat_id = int(tg_id)
        await bot.send_document(
            chat_id,
            BufferedInputFile(docx_bytes, filename=docx_name),
            caption="✅ Word formatdagi rezyume"
        )
        if pdf_bytes:
            await bot.send_document(
                chat_id,
                BufferedInputFile(pdf_bytes, filename=pdf_name),
                caption="✅ PDF formatdagi rezyume"
            )
        else:
            await bot.send_message(chat_id, "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi.")
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=200)

    return {"status": "success"}

# =========================
# TOOLS: caption-based komandalar
# =========================

# PDF SPLIT — caption: /pdf_split 1-3,7
@dp.message(F.document, F.caption.regexp(r"^/pdf_split\s+(.+)$"))
async def h_pdf_split(m: Message, regexp: re.Match):
    ranges = regexp.group(1).strip()
    if m.document.mime_type != "application/pdf":
        return await m.answer("PDF yuboring va captionga: /pdf_split 1-3,7")
    file = await bot.download(m.document)
    out = pdf_split(file.read(), ranges)
    await m.answer_document(BufferedInputFile(out, filename="split.pdf"))

# PDF PAGES → raqamlash — caption: /pagenum [pos]
@dp.message(F.document, F.caption.regexp(r"^/pagenum(?:\s+(\S+))?$"))
async def h_pagenum(m: Message, regexp: re.Match):
    pos = (regexp.group(1) or "bottom-right").strip()
    if m.document.mime_type != "application/pdf":
        return await m.answer("PDF yuboring. Caption: /pagenum bottom-right")
    file = await bot.download(m.document)
    out = pdf_add_page_numbers(file.read(), position=pos)
    await m.answer_document(BufferedInputFile(out, filename="pagenum.pdf"))

# PDF WATERMARK — caption: /watermark Matn
@dp.message(F.document, F.caption.regexp(r"^/watermark\s+(.+)$"))
async def h_watermark(m: Message, regexp: re.Match):
    text = regexp.group(1).strip()
    if m.document.mime_type != "application/pdf":
        return await m.answer("PDF yuboring. Caption: /watermark YOUR_TEXT")
    file = await bot.download(m.document)
    out = pdf_watermark(file.read(), text)
    await m.answer_document(BufferedInputFile(out, filename="watermark.pdf"))

# KONVERT — DOCX/PPTX/XLSX ↔ PDF | PPTX → PNG | PDF → DOCX/PPTX
# caption: /convert pdf | png | docx | pptx
@dp.message(F.document, F.caption.regexp(r"^/convert\s+(\S+)$"))
async def h_convert(m: Message, regexp: re.Match):
    target = regexp.group(1).lower().strip()  # pdf | png | docx | pptx
    name = (m.document.file_name or "").lower()
    in_ext = os.path.splitext(name)[1]

    f = await bot.download(m.document)
    data = f.read()

    try:
        # DOCX/PPTX/XLSX → PDF
        if target == "pdf" and in_ext in {".docx", ".pptx", ".xlsx"}:
            out = soffice_convert(data, in_ext=in_ext, out_ext="pdf")
            if not out:
                return await m.answer("Konvert xatosi (LibreOffice).")
            return await m.answer_document(BufferedInputFile(out, filename="converted.pdf"))

        # PPTX → PNG (ZIP)
        if target == "png" and in_ext == ".pptx":
            zip_bytes = soffice_convert(data, in_ext=".pptx", out_ext="png")
            if not zip_bytes:
                return await m.answer("PPTX → PNG eksport xatosi.")
            return await m.answer_document(BufferedInputFile(zip_bytes, filename="slides_png.zip"))

        # PDF → DOCX/PPTX
        if in_ext == ".pdf" and target in {"docx", "pptx"}:
            out = soffice_convert(data, in_ext=".pdf", out_ext=target)
            if not out:
                return await m.answer(f"PDF → {target.upper()} konvert xatosi.")
            return await m.answer_document(BufferedInputFile(out, filename=f"converted.{target}"))

        return await m.answer(
            "Qo‘llanadigan kombinatsiyalar:\n"
            "• DOCX/PPTX/XLSX → /convert pdf\n"
            "• PPTX → /convert png (ZIP)\n"
            "• PDF → /convert docx\n"
            "• PDF → /convert pptx"
        )
    except Exception as e:
        await m.answer(f"Konvert xatosi: {e}")

# OCR — caption: /ocr [lang]  (default: eng). Faqat PDF.
@dp.message(F.document, F.caption.regexp(r"^/ocr(?:\s+(\S+))?$"))
async def h_ocr(m: Message, regexp: re.Match):
    lang = (regexp.group(1) or "eng").strip()
    if m.document.mime_type != "application/pdf":
        return await m.answer("PDF yuboring. Caption: /ocr [lang] (mas: /ocr eng)")
    file = await bot.download(m.document)
    text = ocr_pdf_to_text(file.read(), lang=lang)
    await m.answer_document(BufferedInputFile(text.encode("utf-8"), filename=f"ocr_{lang}.txt"))

# TARJIMA — caption: /translate [uz|ru|en ...]  (PDF matnini o‘qib tarjima)
@dp.message(F.document, F.caption.regexp(r"^/translate(?:\s+(\S+))?$"))
async def h_translate(m: Message, regexp: re.Match):
    dest = (regexp.group(1) or "uz").strip()
    if m.document.mime_type != "application/pdf":
        return await m.answer("PDF yuboring. Caption: /translate uz")
    file = await bot.download(m.document)
    text = extract_pdf_text(file.read())
    tr = translate_text(text, dest=dest, src_lang="auto")
    await m.answer_document(BufferedInputFile(tr.encode("utf-8"), filename=f"translated_{dest}.txt"))

# =========================
# PDF MERGE sessiya (RAM)
# =========================
import threading
MERGE_BUCKET: dict[int, list[bytes]] = {}
MERGE_LOCK = threading.Lock()

@dp.message(Command("pdf_merge"))
async def h_merge_start(m: Message):
    with MERGE_LOCK:
        MERGE_BUCKET[m.from_user.id] = []
    await m.answer("Merge sessiya boshlandi.\nPDF fayllarni ketma-ket yuboring (captionsiz).\nTugagach: /done")

@dp.message(Command("done"))
async def h_merge_done(m: Message):
    with MERGE_LOCK:
        parts = MERGE_BUCKET.pop(m.from_user.id, [])
    if len(parts) < 2:
        return await m.answer("Kamida 2 ta PDF yuboring.")
    out = pdf_merge(parts)
    await m.answer_document(BufferedInputFile(out, filename="merged.pdf"))

@dp.message(F.document)
async def h_merge_collect(m: Message):
    with MERGE_LOCK:
        bucket = MERGE_BUCKET.get(m.from_user.id)
    if bucket is None:
        return
    if m.document and m.document.mime_type == "application/pdf" and not (m.caption or "").startswith("/"):
        f = await bot.download(m.document)
        with MERGE_LOCK:
            MERGE_BUCKET[m.from_user.id].append(f.read())
        await m.reply("Qo‘shildi ✅")

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

@app.get("/debug/refresh_commands")
async def refresh_commands():
    await set_commands()
    return {"ok": True}
