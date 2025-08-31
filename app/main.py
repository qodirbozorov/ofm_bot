# app/main.py
import os
import io
import re
import json
import sys
import subprocess
import tempfile
import traceback
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import threading

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

        # Session-komandalar:
        BotCommand(command="pdf_split",   description="PDF ajratish (session)"),
        BotCommand(command="pagenum",     description="PDF sahifa raqamlari (session)"),
        BotCommand(command="watermark",   description="PDF watermark (session)"),
        BotCommand(command="convert",     description="Fayl konvertatsiya (session)"),
        BotCommand(command="ocr",         description="Skan PDF → matn (session)"),
        BotCommand(command="translate",   description="PDF matn tarjimasi (session)"),
        BotCommand(command="pdf_merge",   description="PDF birlashtirish (session)"),

        BotCommand(command="done",        description="Joriy sessiyani bajarish"),
        BotCommand(command="cancel",      description="Joriy sessiyani bekor qilish"),
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
        "Mavjud komandalar (session uslubi):\n"
        "• /pdf_split → PDF yuboring, /range 1-3,7 qo‘ying → /done\n"
        "• /pagenum → PDF yuboring, (ixtiyoriy) /pos bottom-right → /done\n"
        "• /watermark → PDF yuboring, /text CONFIDENTIAL → /done\n"
        "• /convert → Fayl yuboring (DOCX/PPTX/XLSX/PDF), /to pdf|png|docx|pptx → /done\n"
        "• /ocr → PDF yuboring, (ixtiyoriy) /lang eng → /done\n"
        "• /translate → PDF yuboring, (ixtiyoriy) /dest uz → /done\n"
        "• /pdf_merge → bir nechta PDF yuboring → /done\n"
        "• /cancel → joriy sessiyani bekor qiladi"
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

def _validate_range_spec(spec: str) -> bool:
    return bool(re.fullmatch(r"\s*\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*\s*", spec))

def pdf_split(src: bytes, range_spec: str) -> bytes:
    r = PdfReader(io.BytesIO(src))
    w = PdfWriter()
    total = len(r.pages)
    for a, b in _parse_ranges(range_spec):
        a = max(1, a); b = min(total, b)
        for i in range(a-1, b):
            w.add_page(r.pages[i])
    buf = io.BytesIO(); w.write(buf); return buf.getvalue()

def pdf_merge(parts: List[bytes]) -> bytes:
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
# RESUME FORMA QABUL QILISH (DB yo‘q)
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
    try:
        rels = json.loads(relatives) if relatives else []
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
    img = None
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

    buf = io.BytesIO()
    doc.render(ctx)
    doc.save(buf)
    docx_bytes = buf.getvalue()

    pdf_bytes = convert_docx_to_pdf(docx_bytes)

    base_name = make_safe_basename(full_name, phone)
    docx_name = f"{base_name}_0.docx"
    pdf_name  = f"{base_name}_0.pdf"
    img_name  = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # Guruhga
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

    # Mijozga
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
@dataclass
class Session:
    mode: str                      # 'split'|'pagenum'|'watermark'|'convert'|'ocr'|'translate'|'merge'
    files: List[Tuple[str, bytes]] = field(default_factory=list)  # (filename, data)
    params: Dict[str, str] = field(default_factory=dict)          # range/pos/text/to/lang/dest

SESSIONS: Dict[int, Session] = {}
S_LOCK = threading.Lock()

def start_session(user_id: int, mode: str) -> None:
    with S_LOCK:
        SESSIONS[user_id] = Session(mode=mode)

def get_session(user_id: int) -> Optional[Session]:
    with S_LOCK:
        return SESSIONS.get(user_id)

def drop_session(user_id: int) -> None:
    with S_LOCK:
        SESSIONS.pop(user_id, None)

def add_file_to_session(user_id: int, filename: str, data: bytes) -> None:
    with S_LOCK:
        s = SESSIONS.get(user_id)
        if s is not None:
            s.files.append((filename, data))

def set_param(user_id: int, key: str, value: str) -> None:
    with S_LOCK:
        s = SESSIONS.get(user_id)
        if s is not None:
            s.params[key] = value

# =========================
# SESSION COMMANDS
# =========================
@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    drop_session(m.from_user.id)
    await m.answer("❌ Sessiya bekor qilindi.")

@dp.message(Command("pdf_split"))
async def cmd_split(m: Message):
    start_session(m.from_user.id, "split")
    await m.answer(
        "✂️ PDF Split sessiyasi boshlandi.\n"
        "1) PDF yuboring (bitta).\n"
        "2) Oraliqni kiriting: /range 1-3,7\n"
        "3) /done bosing."
    )

@dp.message(Command("pagenum"))
async def cmd_pagenum(m: Message):
    start_session(m.from_user.id, "pagenum")
    await m.answer(
        "🔢 Sahifa raqamlari sessiyasi boshlandi.\n"
        "1) PDF yuboring (bitta).\n"
        "2) (Ixtiyoriy) joylashuv: /pos bottom-right | bottom-left | bottom-center | top-right | top-left | top-center\n"
        "3) /done bosing.\n"
        "Standart: bottom-right"
    )

@dp.message(Command("watermark"))
async def cmd_watermark(m: Message):
    start_session(m.from_user.id, "watermark")
    await m.answer(
        "💧 Watermark sessiyasi boshlandi.\n"
        "1) PDF yuboring (bitta).\n"
        "2) Watermark matni: /text YOUR_TEXT\n"
        "3) /done bosing."
    )

@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    start_session(m.from_user.id, "convert")
    await m.answer(
        "🔄 Konvert sessiyasi boshlandi.\n"
        "1) Fayl yuboring (DOCX/PPTX/XLSX/PDF — bitta).\n"
        "2) Target formatni kiriting: /to pdf | png | docx | pptx\n"
        "   • DOCX/PPTX/XLSX → pdf\n"
        "   • PPTX → png (ZIP)\n"
        "   • PDF → docx yoki pptx\n"
        "3) /done bosing."
    )

@dp.message(Command("ocr"))
async def cmd_ocr(m: Message):
    start_session(m.from_user.id, "ocr")
    await m.answer(
        "🧾 OCR sessiyasi boshlandi.\n"
        "1) PDF yuboring (bitta).\n"
        "2) (Ixtiyoriy) til kodi: /lang eng  (eng|rus|uzb va h.k.)\n"
        "3) /done bosing.\n"
        "Standart: eng"
    )

@dp.message(Command("translate"))
async def cmd_translate(m: Message):
    start_session(m.from_user.id, "translate")
    await m.answer(
        "🌐 Tarjima sessiyasi boshlandi.\n"
        "1) PDF yuboring (bitta).\n"
        "2) (Ixtiyoriy) target til: /dest uz  (uz|ru|en ...)\n"
        "3) /done bosing.\n"
        "Standart: uz"
    )

@dp.message(Command("pdf_merge"))
async def cmd_merge(m: Message):
    start_session(m.from_user.id, "merge")
    await m.answer(
        "➕ PDF Merge sessiyasi boshlandi.\n"
        "Ketma-ket bir nechta PDF yuboring (kamida 2 ta), so‘ng /done bosing.\n"
        "Bekor qilish: /cancel"
    )

# Parametr komandalar
@dp.message(F.text.regexp(r"(?i)^/range\s+(.+)$"))
async def param_range(m: Message, regexp: re.Match):
    s = get_session(m.from_user.id)
    if not s or s.mode != "split":
        return await m.answer("Bu parametr faqat /pdf_split sessiyasida ishlaydi.")
    spec = regexp.group(1).strip()
    if not _validate_range_spec(spec):
        return await m.answer("❌ Noto‘g‘ri format. Masalan: /range 1-3,7")
    set_param(m.from_user.id, "range", spec)
    await m.answer(f"✅ Oraliq qabul qilindi: {spec}")

@dp.message(F.text.regexp(r"(?i)^/pos\s+(\S+)$"))
async def param_pos(m: Message, regexp: re.Match):
    s = get_session(m.from_user.id)
    if not s or s.mode != "pagenum":
        return await m.answer("Bu parametr faqat /pagenum sessiyasida ishlaydi.")
    pos = regexp.group(1).strip().lower()
    allowed = {"bottom-right","bottom-left","bottom-center","top-right","top-left","top-center"}
    if pos not in allowed:
        return await m.answer("❌ Noto‘g‘ri qiymat. Ruxsat etilgan: " + ", ".join(sorted(allowed)))
    set_param(m.from_user.id, "pos", pos)
    await m.answer(f"✅ Joylashuv qabul qilindi: {pos}")

@dp.message(F.text.regexp(r"(?i)^/text\s+(.+)$"))
async def param_text(m: Message, regexp: re.Match):
    s = get_session(m.from_user.id)
    if not s or s.mode != "watermark":
        return await m.answer("Bu parametr faqat /watermark sessiyasida ishlaydi.")
    txt = regexp.group(1).strip()
    if not txt:
        return await m.answer("❌ Bo‘sh matn bo‘lmaydi.")
    set_param(m.from_user.id, "text", txt)
    await m.answer("✅ Watermark matni qabul qilindi.")

@dp.message(F.text.regexp(r"(?i)^/to\s+(\S+)$"))
async def param_to(m: Message, regexp: re.Match):
    s = get_session(m.from_user.id)
    if not s or s.mode != "convert":
        return await m.answer("Bu parametr faqat /convert sessiyasida ishlaydi.")
    to = regexp.group(1).strip().lower()
    allowed = {"pdf","png","docx","pptx"}
    if to not in allowed:
        return await m.answer("❌ Ruxsat etilgan: pdf | png | docx | pptx")
    set_param(m.from_user.id, "to", to)
    await m.answer(f"✅ Target format: {to}")

@dp.message(F.text.regexp(r"(?i)^/lang\s+(\S+)$"))
async def param_lang(m: Message, regexp: re.Match):
    s = get_session(m.from_user.id)
    if not s or s.mode != "ocr":
        return await m.answer("Bu parametr faqat /ocr sessiyasida ishlaydi.")
    lang = regexp.group(1).strip()
    set_param(m.from_user.id, "lang", lang)
    await m.answer(f"✅ OCR tili: {lang}")

@dp.message(F.text.regexp(r"(?i)^/dest\s+(\S+)$"))
async def param_dest(m: Message, regexp: re.Match):
    s = get_session(m.from_user.id)
    if not s or s.mode != "translate":
        return await m.answer("Bu parametr faqat /translate sessiyasida ishlaydi.")
    dest = regexp.group(1).strip().lower()
    set_param(m.from_user.id, "dest", dest)
    await m.answer(f"✅ Tarjima tili: {dest}")

# Fayl qabul qilish
@dp.message(F.document)
async def on_document(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return  # sessiya yo'q — sukut

    # Faylni RAMga o‘qish
    f = await bot.download(m.document)
    data = f.read()
    name = (m.document.file_name or "file").lower()
    mt = (m.document.mime_type or "").lower()

    # Validatsiya (modusga qarab)
    if s.mode in {"split","pagenum","watermark","convert","ocr","translate"} and len(s.files) >= 1:
        return await m.answer("❗ Bu sessiya bitta fayl bilan ishlaydi. Agar faylni almashtirmoqchi bo‘lsangiz /cancel qilib qayta boshlang.")

    if s.mode in {"split","pagenum","watermark","ocr","translate","merge"}:
        # PDF talab qilinadi
        if not (name.endswith(".pdf") or mt == "application/pdf"):
            return await m.answer("❌ PDF yuboring.")
    elif s.mode == "convert":
        # DOCX/PPTX/XLSX/PDF
        ok = any(name.endswith(x) for x in [".docx",".pptx",".xlsx",".pdf"])
        if not ok:
            return await m.answer("❌ DOCX/PPTX/XLSX yoki PDF yuboring.")

    add_file_to_session(m.from_user.id, name, data)
    await m.answer("✅ Fayl qabul qilindi.")

# DONE: bajarish
@dp.message(Command("done"))
async def cmd_done(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Sessiya topilmadi. Avval komandani boshlang.")

    try:
        await m.answer("⏳ Qayta ishlanmoqda...")
        mode = s.mode

        # --- SPLIT ---
        if mode == "split":
            if not s.files:
                return await m.answer("❌ PDF yuboring. So‘ng /range ... va /done.")
            rng = s.params.get("range")
            if not rng or not _validate_range_spec(rng):
                return await m.answer("❌ Oraliq kiritilmagan yoki noto‘g‘ri. Masalan: /range 1-3,7")
            out = pdf_split(s.files[0][1], rng)
            drop_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="split.pdf"), caption="✅ Tayyor")

        # --- PAGENUM ---
        if mode == "pagenum":
            if not s.files:
                return await m.answer("❌ PDF yuboring. So‘ng /done.")
            pos = s.params.get("pos", "bottom-right")
            out = pdf_add_page_numbers(s.files[0][1], position=pos)
            drop_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="pagenum.pdf"), caption=f"✅ Tayyor ({pos})")

        # --- WATERMARK ---
        if mode == "watermark":
            if not s.files:
                return await m.answer("❌ PDF yuboring.")
            txt = s.params.get("text")
            if not txt:
                return await m.answer("❌ Watermark matni kiritilmagan. /text YOUR_TEXT")
            out = pdf_watermark(s.files[0][1], txt)
            drop_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="watermark.pdf"), caption="✅ Tayyor")

        # --- CONVERT ---
        if mode == "convert":
            if not s.files:
                return await m.answer("❌ Fayl yuboring (DOCX/PPTX/XLSX/PDF).")
            to = s.params.get("to")
            if not to:
                return await m.answer("❌ Target format kiritilmagan. /to pdf|png|docx|pptx")
            name, data = s.files[0]
            in_ext = os.path.splitext(name)[1]

            # DOCX/PPTX/XLSX → PDF
            if to == "pdf" and in_ext in {".docx",".pptx",".xlsx"}:
                out = soffice_convert(data, in_ext=in_ext, out_ext="pdf")
                if not out:
                    return await m.answer("Konvert xatosi (LibreOffice).")
                drop_session(m.from_user.id)
                return await m.answer_document(BufferedInputFile(out, filename="converted.pdf"), caption="✅ Tayyor")

            # PPTX → PNG (ZIP)
            if to == "png" and in_ext == ".pptx":
                zip_bytes = soffice_convert(data, in_ext=".pptx", out_ext="png")
                if not zip_bytes:
                    return await m.answer("PPTX → PNG eksport xatosi.")
                drop_session(m.from_user.id)
                return await m.answer_document(BufferedInputFile(zip_bytes, filename="slides_png.zip"), caption="✅ Tayyor")

            # PDF → DOCX/PPTX
            if in_ext == ".pdf" and to in {"docx","pptx"}:
                out = soffice_convert(data, in_ext=".pdf", out_ext=to)
                if not out:
                    return await m.answer(f"PDF → {to.upper()} konvert xatosi.")
                drop_session(m.from_user.id)
                return await m.answer_document(BufferedInputFile(out, filename=f"converted.{to}"), caption="✅ Tayyor")

            return await m.answer("❌ Bu kombinatsiya qo‘llanmaydi. /help ni ko‘ring.")

        # --- OCR ---
        if mode == "ocr":
            if not s.files:
                return await m.answer("❌ PDF yuboring.")
            lang = s.params.get("lang","eng")
            text = ocr_pdf_to_text(s.files[0][1], lang=lang)
            drop_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(text.encode("utf-8"), filename=f"ocr_{lang}.txt"), caption="✅ OCR tayyor")

        # --- TRANSLATE ---
        if mode == "translate":
            if not s.files:
                return await m.answer("❌ PDF yuboring.")
            dest = s.params.get("dest","uz")
            text = extract_pdf_text(s.files[0][1]) or ""
            if not text.strip():
                return await m.answer("⚠️ PDF ichidan matn olinmadi. (Skanned bo‘lishi mumkin, avval /ocr qiling.)")
            tr = translate_text(text, dest=dest, src_lang="auto")
            drop_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(tr.encode("utf-8"), filename=f"translated_{dest}.txt"), caption="✅ Tarjima tayyor")

        # --- MERGE ---
        if mode == "merge":
            if len(s.files) < 2:
                return await m.answer("❌ Kamida 2 ta PDF yuboring, so‘ng /done.")
            out = pdf_merge([d for _, d in s.files])
            drop_session(m.from_user.id)
            return await m.answer_document(BufferedInputFile(out, filename="merged.pdf"), caption="✅ Merge tayyor")

        await m.answer("❌ Noma'lum sessiya holati. /cancel qilib qayta boshlang.")
    except Exception as e:
        print("=== SESSION PROCESS ERROR ===", file=sys.stderr)
        print(repr(e), file=sys.stderr)
        traceback.print_exc()
        await m.answer(f"❌ Xatolik: {e}")
    # sessiya tushmay qolsa ham bekor qilish:
    finally:
        drop_session(m.from_user.id)

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
