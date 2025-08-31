# app/main.py
import os
import io
import re
import sys
import json
import math
import time
import tempfile
import traceback
import subprocess
from typing import Optional, Dict, Any, List
from datetime import datetime

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ---- DOCX template
from docxtpl import DocxTemplate, InlineImage
from docx.shared import Mm

# ---- Telegram
from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, InlineKeyboardMarkup, InlineKeyboardButton,
                           WebAppInfo, Update, BotCommand, BufferedInputFile)
from aiogram.filters import Command

# ---- PDF & OCR & Convert helpers
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader

from pdf2image import convert_from_bytes
import pytesseract

# (ixtiyoriy onlayn tarjima – bo‘lmasa ham bot ishlayveradi)
try:
    from googletrans import Translator
except Exception:
    Translator = None


# =========================
# CONFIG: Token/Domain/Group
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"   # trailing slashsiz
GROUP_CHAT_ID = -1003046464831

# =========================
# Aiogram
# =========================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

ACTIVE_USERS = set()

# -------------------------
# Sessiya saqlash (RAM)
# -------------------------
# {user_id: {"op": str, "files": [{"name":..., "bytes":..., "mime":...}], "params": {}}}
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
# FASTAPI app & templates
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
    # Frontdagi JS uchun 200 qaytaramiz (alert chiqishi uchun)
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=200)


@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"


@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)


# =========================
# Utilities
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
    """LibreOffice orqali DOCX -> PDF"""
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "in.docx")
        pdf_path = os.path.join(tmpdir, "in.pdf")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf",
                 "--outdir", tmpdir, docx_path],
                check=True
            )
            with open(pdf_path, "rb") as f:
                return f.read()
        except Exception as e:
            print("DOCX->PDF ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None


def libre_convert(input_bytes: bytes, out_ext: str, in_name: str = "in"):
    """
    LibreOffice generik converter (docx/pptx/xlsx -> pdf, pptx->pdf).
    out_ext misol: 'pdf'
    """
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, f"{in_name}")
        # OFIS fayl turlarini nomidan aniqlash uchun kengaytmasini saqlab qo'yamiz:
        # docx/pptx/xlsx kabi
        if not os.path.splitext(in_path)[1]:
            # agar nomda kengaytma bo‘lmasa, default docx
            in_path += ".bin"
        with open(in_path, "wb") as f:
            f.write(input_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", out_ext, "--outdir", td, in_path],
                check=True
            )
            out_path = os.path.join(td, f"{os.path.splitext(os.path.basename(in_path))[0]}.{out_ext}")
            # ayrim hollarda nom boshqacha chiqishi mumkin, uni topib olamiz:
            if not os.path.exists(out_path):
                for fn in os.listdir(td):
                    if fn.lower().endswith(f".{out_ext}"):
                        out_path = os.path.join(td, fn)
                        break
            with open(out_path, "rb") as f:
                return f.read()
        except Exception as e:
            print("LIBRE CONVERT ERROR:", repr(e), file=sys.stderr)
            traceback.print_exc()
            return None


def pdf_split_bytes(pdf_bytes: bytes, range_str: str) -> Optional[bytes]:
    """
    range_str misol: '1-3,7' — 1..3 va 7-sahifa
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        total = len(reader.pages)

        wanted: List[int] = []
        for chunk in re.split(r"[,\s]+", range_str.strip()):
            if not chunk:
                continue
            if "-" in chunk:
                a, b = chunk.split("-", 1)
                a = max(1, int(a))
                b = min(total, int(b))
                if a <= b:
                    wanted.extend(list(range(a, b + 1)))
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
    """
    Watermark yoki page number uchun generik overlay. pos: bottom/right/top/center variantlari.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()

        for i, page in enumerate(reader.pages, start=1):
            media = page.mediabox
            w = float(media.width)
            h = float(media.height)

            # Har sahifa uchun yagona overlay pdf
            packet = io.BytesIO()
            c = canvas.Canvas(packet, pagesize=(w, h))
            c.setFont("Helvetica", font_size)

            text_to_draw = text.replace("{page}", str(i))

            # joylashuv
            margin = 20
            tw = c.stringWidth(text_to_draw, "Helvetica", font_size)
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

            c.drawString(x, y, text_to_draw)
            c.save()

            packet.seek(0)
            overlay = PdfReader(packet)
            overlay_page = overlay.pages[0]
            page.merge_page(overlay_page)
            writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:
        print("PDF OVERLAY ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return None


def ocr_pdf_to_text(pdf_bytes: bytes, lang: str = "eng") -> str:
    """
    PDF -> images -> tesseract -> text
    """
    try:
        images = convert_from_bytes(pdf_bytes, dpi=200)
        texts = []
        for img in images:
            txt = pytesseract.image_to_string(img, lang=lang)
            texts.append(txt)
        return "\n\n".join(texts).strip()
    except Exception as e:
        print("OCR ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        return ""


# =========================
# Resume (docx template -> docx/pdf)
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

    # ixtiyoriy foto (InlineImage)
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

    # DOCX -> PDF
    pdf_bytes = convert_docx_to_pdf(docx_bytes)

    # Fayl nomlari
    base_name = make_safe_basename(full_name, phone)
    docx_name = f"{base_name}_0.docx"
    pdf_name = f"{base_name}_0.pdf"
    img_name = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # Guruhga: rasm + JSON (alohida hujjatlar)
    try:
        if img_bytes:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img_bytes, filename=img_name),
                caption=f"🆕 Forma: {full_name}\n📞 {phone}\n👤 TG: {tg_id}"
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
            caption=f"📄 JSON: {full_name}"
        )
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()

    # Mijozga: DOCX + PDF
    try:
        chat_id = int(tg_id)
        await bot.send_document(chat_id, BufferedInputFile(docx_bytes, filename=docx_name),
                                caption="✅ Word formatdagi rezyume")
        if pdf_bytes:
            await bot.send_document(chat_id, BufferedInputFile(pdf_bytes, filename=pdf_name),
                                    caption="✅ PDF formatdagi rezyume")
        else:
            await bot.send_message(chat_id, "⚠️ PDF konvertda xatolik, hozircha faqat Word yuborildi.")
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=200)

    # WebApp yopish signalini beradigan JSON
    return {"status": "success", "close": True}


# =========================
# Start / Help / New_resume
# =========================
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
        "Asosiy komandalar:\n"
        "/new_resume – Web formani ochish\n"
        "/pdf_split – PDFdan sahifalarni ajratish\n"
        "/pdf_merge – PDFlarni qo‘shish\n"
        "/pagenum – PDFga sahifa raqami qo‘shish\n"
        "/watermark – PDFga watermark qo‘shish\n"
        "/ocr – Skan PDFdan matn chiqarish\n"
        "/convert – DOCX/PPTX/XLSX/PDF konvertatsiya\n"
        "/translate – PDF matnini tarjima qilish\n"
        "/status – Sessiya holati\n"
        "/cancel – Sessiyani bekor qilish\n"
        "/done – Amalni yakunlash"
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
# Session boshqaruv komandalar
# =========================
async def show_status(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("❌ Sessiya yo‘q. /pdf_split, /pdf_merge, /convert … dan birini boshlab yuboring.")
    files_info = "—"
    if s["files"]:
        files_info = "\n".join([f" {i+1}) {f['name']} ({human_size(len(f['bytes']))})"
                                for i, f in enumerate(s["files"])])
    params_info = "Parametrlar hali berilmagan" if not s["params"] else json.dumps(s["params"], ensure_ascii=False)
    await m.answer(
        f"🧩 Jarayon: {s['op']}\n"
        f"📎 Fayllar: {len(s['files'])} ta\n{files_info}\n"
        f"⚙️ Parametrlar: {params_info}\n"
        f"Yakunlash: /done   |   Bekor: /cancel"
    )


@dp.message(Command("status"))
async def cmd_status(m: Message):
    await show_status(m)


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    clear_session(m.from_user.id)
    await m.answer("❌ Session bekor qilindi.")


# ---- Start session commands
@dp.message(Command("pdf_split"))
async def cmd_split(m: Message):
    new_session(m.from_user.id, "split")
    await m.answer(
        "✂️ PDF Split sessiyasi boshlandi.\n"
        "1) PDF fayl yuboring.\n"
        "2) Oraliq ko‘rsating: /range 1-3,7\n"
        "Tugash: /done   |   Holat: /status   |   Bekor: /cancel"
    )


@dp.message(Command("pdf_merge"))
async def cmd_merge(m: Message):
    new_session(m.from_user.id, "merge")
    await m.answer(
        "🧷 PDF Merge sessiyasi boshlandi.\n"
        "Ketma-ket bir nechta PDF yuboring (har safar qo‘shiladi).\n"
        "Tugash: /done   |   Holat: /status   |   Bekor: /cancel"
    )


@dp.message(Command("pagenum"))
async def cmd_pagenum(m: Message):
    new_session(m.from_user.id, "pagenum")
    await m.answer(
        "🔢 Sahifa raqami sessiyasi.\n"
        "1) PDF yuboring.\n"
        "2) Pozitsiyani bering: /pos bottom-right (yoki top-left/top-center/...)\n"
        "Tugash: /done   |   Holat: /status"
    )


@dp.message(Command("watermark"))
async def cmd_watermark(m: Message):
    new_session(m.from_user.id, "watermark")
    await m.answer(
        "💧 Watermark sessiyasi.\n"
        "1) PDF yuboring.\n"
        "2) Matn: /wm Confidential\n"
        "3) Pozitsiya ixtiyoriy: /pos bottom-right\n"
        "Tugash: /done   |   Holat: /status"
    )


@dp.message(Command("ocr"))
async def cmd_ocr(m: Message):
    new_session(m.from_user.id, "ocr")
    await m.answer(
        "🪄 OCR sessiyasi.\n"
        "1) Skan qilingan PDF yuboring.\n"
        "2) Tesseract til kodi: /lang eng  (uzb uchun 'uzb', rus 'rus', ...)\n"
        "Tugash: /done   |   Holat: /status"
    )


@dp.message(Command("translate"))
async def cmd_translate(m: Message):
    new_session(m.from_user.id, "translate")
    await m.answer(
        "🌐 Tarjima sessiyasi.\n"
        "1) PDF yuboring (matnli yoki OCR qilingan bo‘lsa yaxshi).\n"
        "2) Maqsad til: /to uz  (misol: uz, ru, en)\n"
        "Tugash: /done   |   Holat: /status"
    )


@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    new_session(m.from_user.id, "convert")
    await m.answer(
        "🔁 Konvert sessiyasi boshlandi.\n"
        "1) Bitta fayl yuboring (DOCX/PPTX/XLSX yoki PDF; PPTX→PNG uchun PPTX yuboring).\n"
        "2) Maqsad format: /target pdf | png | docx | pptx\n"
        "Qoida:\n"
        "  • DOCX/PPTX/XLSX → PDF : /target pdf\n"
        "  • PPTX → PNG (1-slayd) : /target png\n"
        "  • PDF → PNG (1-sahifa) : /target png\n"
        "  • PDF → PPTX/DOCX ❌ qo‘llanmaydi\n"
        "Tugash: /done   |   Holat: /status"
    )


# =========================
# Parametr komandalar (versiya-agnostik)
# =========================
RE_RANGE  = re.compile(r"^/range\s+(.+)$")
RE_POS    = re.compile(r"^/pos\s+(\S+)$")
RE_WM     = re.compile(r"^/wm\s+(.+)$")
RE_TARGET = re.compile(r"^/target\s+(\S+)$")
RE_LANG   = re.compile(r"^/lang\s+(\S+)$")
RE_TO     = re.compile(r"^/to\s+(\S+)$")
RE_MISS   = re.compile(r"^/(range|pos|wm|target|lang|to)\s*$")


def _get_match(message: Message, data: dict, pattern: re.Pattern) -> Optional[re.Match]:
    mobj = data.get("regexp") or data.get("match")
    if mobj:
        return mobj
    txt = (message.text or "").strip()
    return pattern.match(txt)


@dp.message(F.text.regexp(RE_RANGE))
async def param_range(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "split":
        return await m.answer("Bu parametr faqat /pdf_split sessiyasida ishlaydi.")
    mobj = _get_match(m, data, RE_RANGE)
    if not mobj:
        return await m.answer("Oraliq formati: /range 1-3,7")
    s["params"]["range"] = mobj.group(1).strip()
    await m.answer("✅ Oraliq qabul qilindi. /status yoki /done")


@dp.message(F.text.regexp(RE_POS))
async def param_pos(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] not in {"pagenum", "watermark"}:
        return await m.answer("Bu parametr /pagenum yoki /watermark sessiyalarida ishlaydi.")
    mobj = _get_match(m, data, RE_POS)
    if not mobj:
        return await m.answer("Pozitsiya: /pos bottom-right")
    pos = mobj.group(1).strip().lower()
    allowed = {"bottom-right","bottom-left","bottom-center","top-right","top-left","top-center"}
    if pos not in allowed:
        return await m.answer("Noto‘g‘ri pozitsiya. Ruxsat: " + ", ".join(sorted(allowed)))
    s["params"]["pos"] = pos
    await m.answer("✅ Joylashuv qabul qilindi. /status yoki /done")


@dp.message(F.text.regexp(RE_WM))
async def param_wm(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "watermark":
        return await m.answer("Bu parametr faqat /watermark sessiyasida.")
    mobj = _get_match(m, data, RE_WM)
    if not mobj:
        return await m.answer("Matn: /wm Confidential")
    text = mobj.group(1).strip()
    if not text:
        return await m.answer("Matn bo‘sh bo‘lmasin.")
    s["params"]["wm"] = text[:100]
    await m.answer("✅ Watermark matni qabul qilindi.")


@dp.message(F.text.regexp(RE_TARGET))
async def param_target(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "convert":
        return await m.answer("Bu parametr faqat /convert sessiyasida.")
    mobj = _get_match(m, data, RE_TARGET)
    if not mobj:
        return await m.answer("Maqsad format: /target pdf | png | docx | pptx")
    target = mobj.group(1).strip().lower()
    if target not in {"pdf","png","docx","pptx"}:
        return await m.answer("Maqsad format: pdf | png | docx | pptx")
    s["params"]["target"] = target
    await m.answer("✅ Maqsad format qabul qilindi.")


@dp.message(F.text.regexp(RE_LANG))
async def param_lang(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "ocr":
        return await m.answer("Bu parametr faqat /ocr sessiyasida.")
    mobj = _get_match(m, data, RE_LANG)
    if not mobj:
        return await m.answer("Til kodi: /lang eng")
    s["params"]["lang"] = mobj.group(1).strip()
    await m.answer("✅ Til qabul qilindi.")


@dp.message(F.text.regexp(RE_TO))
async def param_to(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "translate":
        return await m.answer("Bu parametr faqat /translate sessiyasida.")
    mobj = _get_match(m, data, RE_TO)
    if not mobj:
        return await m.answer("Maqsad til: /to uz")
    s["params"]["to"] = mobj.group(1).strip()
    await m.answer("✅ Maqsad til qabul qilindi.")


@dp.message(F.text.regexp(RE_MISS))
async def param_missing(m: Message, **data):
    mobj = _get_match(m, data, RE_MISS)
    cmd = mobj.group(1) if mobj else ""
    examples = {
        "range":  "Masalan: /range 1-3,7",
        "pos":    "Masalan: /pos bottom-right",
        "wm":     "Masalan: /wm Confidential",
        "target": "Masalan: /target pdf | png | docx | pptx",
        "lang":   "Masalan: /lang eng",
        "to":     "Masalan: /to uz",
    }
    await m.answer(f"Parametr yetishmayapti. {examples.get(cmd, '')}")


# =========================
# Fayl qabul qilish (barqaror download)
# =========================
@dp.message(F.document)
async def collect_file(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return  # sessiya bo'lmasa jim

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
        return await m.reply("❌ Faylni qabul qilib bo‘lmadi. Qayta urinib ko‘ring.")

    op = s["op"]
    if op == "merge":
        if mime != "application/pdf":
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi.")
        s["files"].append({"name": name, "bytes": data, "mime": mime})
        return await m.reply(f"Qo‘shildi ✅  ({name})  — jami: {len(s['files'])}")

    if op in {"split", "pagenum", "watermark", "ocr", "translate", "convert"}:
        s["files"] = [{"name": name, "bytes": data, "mime": mime}]
        if op in {"split", "pagenum", "watermark", "ocr", "translate"} and mime != "application/pdf":
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi.")
        await m.reply(f"Fayl qabul qilindi: {name} ✅  (/status yoki parametr yuboring, keyin /done)")


# =========================
# /done – bajarish
# =========================
@dp.message(Command("done"))
async def cmd_done(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Sessiya yo‘q.")

    op = s["op"]
    files = s["files"]
    p = s["params"]

    try:
        if op == "split":
            if not files:
                return await m.answer("PDF yuboring.")
            if "range" not in p:
                return await m.answer("Oraliq kerak: /range 1-3,7")
            out = pdf_split_bytes(files[0]["bytes"], p["range"])
            if not out:
                return await m.answer("Ajratishda xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="split.pdf"),
                                    caption="✅ Split tayyor")
            clear_session(m.from_user.id)
            return

        if op == "merge":
            if len(files) < 2:
                return await m.answer("Hech bo‘lmasa 2 ta PDF yuboring.")
            out = pdf_merge_bytes([f["bytes"] for f in files])
            if not out:
                return await m.answer("Merge xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="merge.pdf"),
                                    caption="✅ Merge tayyor")
            clear_session(m.from_user.id)
            return

        if op == "pagenum":
            if not files:
                return await m.answer("PDF yuboring.")
            pos = p.get("pos", "bottom-right")
            out = pdf_overlay_text(files[0]["bytes"], text="{page}", pos=pos, font_size=10)
            if not out:
                return await m.answer("Sahifa raqami qo‘shishda xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="pagenum.pdf"),
                                    caption="✅ Sahifa raqamlari qo‘shildi")
            clear_session(m.from_user.id)
            return

        if op == "watermark":
            if not files:
                return await m.answer("PDF yuboring.")
            wm = p.get("wm")
            if not wm:
                return await m.answer("Matn bering: /wm Confidential")
            pos = p.get("pos", "bottom-right")
            out = pdf_overlay_text(files[0]["bytes"], text=wm, pos=pos, font_size=14)
            if not out:
                return await m.answer("Watermarkda xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="watermark.pdf"),
                                    caption="✅ Watermark qo‘shildi")
            clear_session(m.from_user.id)
            return

        if op == "ocr":
            if not files:
                return await m.answer("PDF yuboring.")
            lang = p.get("lang", "eng")
            txt = ocr_pdf_to_text(files[0]["bytes"], lang=lang)
            if not txt:
                return await m.answer("OCR natijasi bo‘sh. Til kodini tekshiring (/lang eng).")
            await bot.send_document(
                m.chat.id,
                BufferedInputFile(txt.encode("utf-8"), filename="ocr.txt"),
                caption=f"✅ OCR tayyor (lang={lang})"
            )
            clear_session(m.from_user.id)
            return

        if op == "translate":
            if not files:
                return await m.answer("PDF yuboring.")
            to = p.get("to", "uz")
            # PDFdan matn olish (soddalashtirilgan: OCR emas, text layer bor deb faraz)
            reader = PdfReader(io.BytesIO(files[0]["bytes"]))
            src_text = "\n\n".join([page.extract_text() or "" for page in reader.pages]).strip()
            if not src_text:
                return await m.answer("PDFdan matn olinmadi. Avval /ocr bilan text oling.")
            out_text = src_text
            if Translator:
                try:
                    tr = Translator()
                    out_text = tr.translate(src_text, dest=to).text
                except Exception as e:
                    print("TRANSLATE ERROR:", repr(e), file=sys.stderr)
            await bot.send_document(
                m.chat.id,
                BufferedInputFile(out_text.encode("utf-8"), filename=f"translate_{to}.txt"),
                caption=f"✅ Tarjima tayyor (-> {to})"
            )
            clear_session(m.from_user.id)
            return

        if op == "convert":
            if not files:
                return await m.answer("Fayl yuboring.")
            target = p.get("target")
            if target not in {"pdf", "png", "docx", "pptx"}:
                return await m.answer("Maqsad format: /target pdf|png|docx|pptx")

            name = files[0]["name"].lower()
            data = files[0]["bytes"]

            # DOCX/PPTX/XLSX -> PDF
            if target == "pdf" and name.endswith((".docx", ".pptx", ".xlsx")):
                out = libre_convert(data, "pdf", in_name=name)
                if not out:
                    return await m.answer("Konvert xatolik.")
                await bot.send_document(m.chat.id, BufferedInputFile(out, filename=f"{os.path.splitext(name)[0]}.pdf"),
                                        caption="✅ PDF tayyor")
                clear_session(m.from_user.id)
                return

            # PPTX -> PNG (1-slayd), PDF -> PNG (1-sahifa)
            if target == "png" and (name.endswith(".pptx") or name.endswith(".pdf")):
                # Agar PPTX bo'lsa avval PDFga aylantirib olamiz
                if name.endswith(".pptx"):
                    pdf = libre_convert(data, "pdf", in_name=name)
                    if not pdf:
                        return await m.answer("PPTX->PDF xatolik.")
                    pages = convert_from_bytes(pdf, dpi=180, first_page=1, last_page=1)
                else:
                    pages = convert_from_bytes(data, dpi=180, first_page=1, last_page=1)

                buf = io.BytesIO()
                pages[0].save(buf, format="PNG")
                await bot.send_document(m.chat.id,
                                        BufferedInputFile(buf.getvalue(), filename=f"{os.path.splitext(name)[0]}_1.png"),
                                        caption="✅ PNG (1-sahifa/slayd)")
                clear_session(m.from_user.id)
                return

            # PDF -> DOCX/PPTX qo‘llanmaydi
            return await m.answer("Bu yo‘nalish hozircha qo‘llanmaydi yoki noto‘g‘ri maqsad format.")

    except Exception as e:
        print("DONE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        await m.answer("❌ Jarayon davomida xatolik yuz berdi. /status bilan tekshiring yoki /cancel qiling.")
        return


# =========================
# Bot commands list (menu)
# =========================
async def _set_commands():
    cmds = [
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="new_resume", description="Web rezyume forma"),
        BotCommand(command="help", description="Yordam"),
        BotCommand(command="pdf_split", description="PDF sahifalarini ajratish"),
        BotCommand(command="pdf_merge", description="Bir nechta PDFni qo‘shish"),
        BotCommand(command="pagenum", description="PDFga sahifa raqami qo‘shish"),
        BotCommand(command="watermark", description="PDFga watermark qo‘shish"),
        BotCommand(command="ocr", description="Skan PDFdan matn chiqarish"),
        BotCommand(command="convert", description="Fayl konvertatsiya"),
        BotCommand(command="translate", description="PDF matnini tarjima"),
        BotCommand(command="status", description="Sessiya holati"),
        BotCommand(command="done", description="Yakunlash"),
        BotCommand(command="cancel", description="Bekor qilish"),
    ]
    await bot.set_my_commands(cmds)
    print("✅ Bot commands list yangilandi")


@app.on_event("startup")
async def on_startup():
    try:
        await _set_commands()
    except Exception as e:
        print("SET COMMANDS ERROR:", repr(e), file=sys.stderr)


# =========================
# Webhook endpoints
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
# Debug
# =========================
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}


@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}
