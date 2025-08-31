# app/main.py
import os
import io
import re
import json
import sys
import subprocess
import tempfile
import traceback
import threading
from typing import Optional, Literal
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
        # Session-based tools:
        BotCommand(command="pdf_split",   description="PDF ajratish (session)"),
        BotCommand(command="pdf_merge",   description="PDF birlashtirish (session)"),
        BotCommand(command="pagenum",     description="PDF sahifa raqami (session)"),
        BotCommand(command="watermark",   description="PDF watermark (session)"),
        BotCommand(command="convert",     description="DOCX/PPTX/XLSX↔PDF | PPTX→PNG | PDF→DOCX/PPTX"),
        BotCommand(command="ocr",         description="Skan PDF → matn (session)"),
        BotCommand(command="translate",   description="PDF matn tarjimasi (session)"),
        BotCommand(command="status",      description="Session holati"),
        BotCommand(command="cancel",      description="Sessionni bekor qilish"),
        BotCommand(command="done",        description="Sessionni yakunlash"),
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
    await m.answer(
        "Session uslubi:\n"
        "1) /pdf_split | /pdf_merge | /pagenum | /watermark | /convert | /ocr | /translate\n"
        "2) Fayl(lar)ni yuborasiz (ko‘rsatmaga qarang)\n"
        "3) Zarur bo‘lsa qo‘shimcha parametrlar: /range, /pos, /wm, /target, /lang, /to\n"
        "4) /done — natijani olish\n"
        "❌ Bekor qilish: /cancel\n"
        "ℹ️ Holat: /status"
    )

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

def pdf_split_validate(src: bytes, range_spec: str) -> tuple[bool, str]:
    try:
        r = PdfReader(io.BytesIO(src))
        total = len(r.pages)
    except Exception:
        return False, "PDF o‘qib bo‘lmadi."
    try:
        ranges = _parse_ranges(range_spec)
    except Exception:
        return False, "Oraliq formati noto‘g‘ri. Masalan: 1-3,7"
    for a, b in ranges:
        if a < 1 or b < 1 or a > b:
            return False, f"Oraliq xato: {a}-{b}"
        if b > total:
            return False, f"Sahifa {b} mavjud emas. PDF’da {total} sahifa bor."
    return True, ""

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
    c.drawCentredString(0, 0, text[:100])
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
# SESSION MANAGER
# =========================
SessionOp = Literal["split", "merge", "pagenum", "watermark", "convert", "ocr", "translate"]
SESSIONS: dict[int, dict] = {}
SESS_LOCK = threading.Lock()

def start_session(uid: int, op: SessionOp):
    with SESS_LOCK:
        SESSIONS[uid] = {
            "op": op,
            "files": [],   # list of dict{name, bytes, mime}
            "params": {},  # op ga qarab: range/pos/wm/target/lang/to
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

def get_session(uid: int) -> Optional[dict]:
    with SESS_LOCK:
        return SESSIONS.get(uid)

def clear_session(uid: int):
    with SESS_LOCK:
        SESSIONS.pop(uid, None)

def session_summary(s: dict) -> str:
    files = s["files"]
    params = s["params"]
    lines = [f"🔧 Jarayon: {s['op']}"]
    if files:
        lines.append(f"📎 Fayllar: {len(files)} ta")
        for i, f in enumerate(files, 1):
            lines.append(f"  {i}) {f['name']} ({len(f['bytes'])//1024} KB)")
    else:
        lines.append("📎 Fayl hali yuborilmadi")
    if params:
        lines.append("⚙️ Parametrlar:")
        for k, v in params.items():
            lines.append(f"  • {k}: {v}")
    else:
        lines.append("⚙️ Parametrlar hali berilmagan")
    lines.append("Yakunlash: /done   |   Bekor: /cancel")
    return "\n".join(lines)

# =========================
# SESSION: Entry komandalar
# =========================
@dp.message(Command("pdf_merge"))
async def sess_merge(m: Message):
    start_session(m.from_user.id, "merge")
    await m.answer(
        "🧩 PDF birlashtirish sessiyasi boshlandi.\n"
        "Bir nechta PDF faylni ketma-ket yuboring (captionsiz).\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("pdf_split"))
async def sess_split(m: Message):
    start_session(m.from_user.id, "split")
    await m.answer(
        "✂️ PDF ajratish sessiyasi boshlandi.\n"
        "1) Bitta PDF fayl yuboring.\n"
        "2) Oraliq kiriting: /range 1-3,7\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("pagenum"))
async def sess_pagenum(m: Message):
    start_session(m.from_user.id, "pagenum")
    await m.answer(
        "🔢 PDF sahifa raqami sessiyasi boshlandi.\n"
        "1) Bitta PDF fayl yuboring.\n"
        "2) Ixtiyoriy joylashuv: /pos bottom-right | bottom-left | bottom-center | top-right | top-left | top-center\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("watermark"))
async def sess_watermark(m: Message):
    start_session(m.from_user.id, "watermark")
    await m.answer(
        "💧 PDF watermark sessiyasi boshlandi.\n"
        "1) Bitta PDF fayl yuboring.\n"
        "2) Watermark matnini kiriting: /wm YOUR_TEXT\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("convert"))
async def sess_convert(m: Message):
    start_session(m.from_user.id, "convert")
    await m.answer(
        "🔁 Konvert sessiyasi boshlandi.\n"
        "1) Bitta fayl yuboring (DOCX/PPTX/XLSX yoki PDF yoki PPTX → PNG uchun PPTX).\n"
        "2) Maqsad format: /target pdf | png | docx | pptx\n"
        "Qoida:\n"
        " • DOCX/PPTX/XLSX → PDF: /target pdf\n"
        " • PPTX → PNG (ZIP): /target png\n"
        " • PDF → DOCX: /target docx\n"
        " • PDF → PPTX: /target pptx\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("ocr"))
async def sess_ocr(m: Message):
    start_session(m.from_user.id, "ocr")
    await m.answer(
        "🔎 OCR sessiyasi boshlandi.\n"
        "1) Bitta PDF fayl yuboring.\n"
        "2) Til (ixtiyoriy): /lang eng  (mas: uzb, rus — o‘rnatilgan bo‘lishi kerak)\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("translate"))
async def sess_translate(m: Message):
    start_session(m.from_user.id, "translate")
    await m.answer(
        "🌐 Tarjima sessiyasi boshlandi.\n"
        "1) Bitta PDF fayl yuboring.\n"
        "2) Maqsad til (ixtiyoriy): /to uz  (yoki ru, en, ...)\n"
        "Tugagach: /done  |  Bekor: /cancel  |  Holat: /status"
    )

@dp.message(Command("cancel"))
async def sess_cancel(m: Message):
    clear_session(m.from_user.id)
    await m.answer("❌ Session bekor qilindi.")

@dp.message(Command("status"))
async def sess_status(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Session yo‘q. Boshlash: /pdf_merge, /pdf_split, /pagenum, /watermark, /convert, /ocr, /translate")
    await m.answer(session_summary(s))

# =========================
# SESSION: Parametr komandalar
# =========================
@dp.message(F.text.regexp(r"^/range\s+(.+)$"))
async def param_range(m: Message, match: re.Match):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "split":
        return await m.answer("Bu parametr faqat /pdf_split sessiyasida ishlaydi.")
    s["params"]["range"] = match.group(1).strip()
    await m.answer("✅ Oraliq qabul qilindi. /status yoki /done")

@dp.message(F.text.regexp(r"^/pos\s+(\S+)$"))
async def param_pos(m: Message, match: re.Match):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "pagenum":
        return await m.answer("Bu parametr faqat /pagenum sessiyasida ishlaydi.")
    pos = match.group(1).strip().lower()
    allowed = {"bottom-right","bottom-left","bottom-center","top-right","top-left","top-center"}
    if pos not in allowed:
        return await m.answer("Noto‘g‘ri pozitsiya. Ruxsat etilganlar: " + ", ".join(sorted(allowed)))
    s["params"]["pos"] = pos
    await m.answer("✅ Joylashuv qabul qilindi. /status yoki /done")

@dp.message(F.text.regexp(r"^/wm\s+(.+)$"))
async def param_wm(m: Message, match: re.Match):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "watermark":
        return await m.answer("Bu parametr faqat /watermark sessiyasida ishlaydi.")
    text = match.group(1).strip()
    if not text:
        return await m.answer("Matn bo‘sh bo‘lmasin.")
    s["params"]["wm"] = text[:100]
    await m.answer("✅ Watermark matni qabul qilindi. /status yoki /done")

@dp.message(F.text.regexp(r"^/target\s+(\S+)$"))
async def param_target(m: Message, match: re.Match):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "convert":
        return await m.answer("Bu parametr faqat /convert sessiyasida ishlaydi.")
    target = match.group(1).strip().lower()
    if target not in {"pdf","png","docx","pptx"}:
        return await m.answer("Maqsad format: pdf | png | docx | pptx")
    s["params"]["target"] = target
    await m.answer("✅ Maqsad format qabul qilindi. /status yoki /done")

@dp.message(F.text.regexp(r"^/lang\s+(\S+)$"))
async def param_lang(m: Message, match: re.Match):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "ocr":
        return await m.answer("Bu parametr faqat /ocr sessiyasida ishlaydi.")
    s["params"]["lang"] = match.group(1).strip()
    await m.answer("✅ Til qabul qilindi. /status yoki /done")

@dp.message(F.text.regexp(r"^/to\s+(\S+)$"))
async def param_to(m: Message, match: re.Match):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "translate":
        return await m.answer("Bu parametr faqat /translate sessiyasida ishlaydi.")
    s["params"]["to"] = match.group(1).strip()
    await m.answer("✅ Maqsad til qabul qilindi. /status yoki /done")

# =========================
# SESSION: Fayl qabul qilish
# =========================
@dp.message(F.document)
async def collect_file(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return  # session yo'q — boshqa handlerlarga qoldiramiz (yoki jim)

    # faylni yuklab olamiz
    f = await bot.download(m.document)
    data = f.read()
    name = m.document.file_name or "file.bin"
    mime = m.document.mime_type or "application/octet-stream"

    op = s["op"]
    if op == "merge":
        if mime != "application/pdf":
            return await m.reply("Faqat PDF qabul qilinadi.")
        s["files"].append({"name": name, "bytes": data, "mime": mime})
        return await m.reply(f"Qo‘shildi ✅  ({name})  — jami: {len(s['files'])}")

    if op in {"split","pagenum","watermark","ocr","translate","convert"}:
        if s["files"]:
            s["files"] = []  # oxirgi fayl dolzarb — eski faylni almashtiramiz
        s["files"].append({"name": name, "bytes": data, "mime": mime})
        # tezkor tip-tekshiruv
        if op in {"split","pagenum","watermark","ocr","translate"} and mime != "application/pdf":
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi. Boshqa fayl yuboring yoki /cancel.")
        await m.reply(f"Fayl qabul qilindi: {name} ✅  (/status yoki parametr yuboring, keyin /done)")
        return

# =========================
# SESSION: Yakunlash (/done)
# =========================
@dp.message(Command("done"))
async def sess_done(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Session yo‘q. Boshlash: /pdf_merge, /pdf_split, /pagenum, /watermark, /convert, /ocr, /translate")

    op = s["op"]
    files = s["files"]
    params = s["params"]

    try:
        if op == "merge":
            if len(files) < 2:
                return await m.answer("Kamida 2 ta PDF kerak.")
            await m.answer("⏳ Birlashtirilmoqda...")
            out = pdf_merge([f["bytes"] for f in files])
            clear_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="merged.pdf"))

        if op == "split":
            if not files:
                return await m.answer("Bitta PDF yuboring.")
            if "range" not in params:
                return await m.answer("Oraliq belgilang: /range 1-3,7")
            ok, msg = pdf_split_validate(files[0]["bytes"], params["range"])
            if not ok:
                return await m.answer("❌ " + msg)
            await m.answer("⏳ Ajratilmoqda...")
            out = pdf_split(files[0]["bytes"], params["range"])
            clear_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="split.pdf"))

        if op == "pagenum":
            if not files:
                return await m.answer("Bitta PDF yuboring.")
            pos = params.get("pos", "bottom-right")
            await m.answer("⏳ Raqamlar qo‘shilmoqda...")
            out = pdf_add_page_numbers(files[0]["bytes"], position=pos)
            clear_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="pagenum.pdf"))

        if op == "watermark":
            if not files:
                return await m.answer("Bitta PDF yuboring.")
            if "wm" not in params:
                return await m.answer("Watermark matnini belgilang: /wm YOUR_TEXT")
            await m.answer("⏳ Watermark qo‘shilmoqda...")
            out = pdf_watermark(files[0]["bytes"], params["wm"])
            clear_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="watermark.pdf"))

        if op == "convert":
            if not files:
                return await m.answer("Bitta fayl yuboring.")
            if "target" not in params:
                return await m.answer("Maqsad formatni belgilang: /target pdf|png|docx|pptx")
            target = params["target"]
            name = files[0]["name"].lower()
            in_ext = os.path.splitext(name)[1]
            mime = files[0]["mime"]
            # Valid kombinatsiyalar:
            # DOCX/PPTX/XLSX -> PDF
            if target == "pdf" and in_ext in {".docx",".pptx",".xlsx"}:
                await m.answer("⏳ Konvert qilinmoqda (→ PDF)...")
                out = soffice_convert(files[0]["bytes"], in_ext=in_ext, out_ext="pdf")
                if not out:
                    return await m.answer("Konvert xatosi (LibreOffice).")
                clear_session(m.from_user.id)
                return await m.answer_document(BufferedInputFile(out, filename="converted.pdf"))
            # PPTX -> PNG (ZIP)
            if target == "png" and in_ext == ".pptx":
                await m.answer("⏳ Slaydlar PNG'ga eksport qilinmoqda...")
                zip_bytes = soffice_convert(files[0]["bytes"], in_ext=".pptx", out_ext="png")
                if not zip_bytes:
                    return await m.answer("PPTX → PNG eksport xatosi.")
                clear_session(m.from_user.id)
                return await m.answer_document(BufferedInputFile(zip_bytes, filename="slides_png.zip"))
            # PDF -> DOCX/PPTX
            if in_ext == ".pdf" and target in {"docx","pptx"}:
                await m.answer(f"⏳ Konvert qilinmoqda (PDF → {target.upper()})...")
                out = soffice_convert(files[0]["bytes"], in_ext=".pdf", out_ext=target)
                if not out:
                    return await m.answer(f"PDF → {target.upper()} konvert xatosi.")
                clear_session(m.from_user.id)
                return await m.answer_document(BufferedInputFile(out, filename=f"converted.{target}"))
            return await m.answer("Noto‘g‘ri format kombinatsiyasi. /status ko‘ring va /target to‘g‘ri ekanini tekshiring.")

        if op == "ocr":
            if not files:
                return await m.answer("Bitta PDF yuboring.")
            lang = params.get("lang", "eng")
            await m.answer(f"⏳ OCR bajarilmoqda (lang={lang})...")
            text = ocr_pdf_to_text(files[0]["bytes"], lang=lang)
            clear_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(text.encode("utf-8"), filename=f"ocr_{lang}.txt"))

        if op == "translate":
            if not files:
                return await m.answer("Bitta PDF yuboring.")
            dest = params.get("to", "uz")
            await m.answer(f"⏳ PDF matni olinmoqda va tarjima qilinmoqda (→ {dest})...")
            text = extract_pdf_text(files[0]["bytes"])
            tr = translate_text(text, dest=dest, src_lang="auto")
            clear_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(tr.encode("utf-8"), filename=f"translated_{dest}.txt"))

        return await m.answer("Noma'lum session turi.")
    except Exception as e:
        traceback.print_exc()
        return await m.answer(f"❌ Xatolik: {e}")

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
