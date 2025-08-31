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
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    Update, BotCommand, BufferedInputFile
)
from aiogram.filters import Command

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE = "https://ofmbot-production.up.railway.app"
GROUP_CHAT_ID = -1003046464831

# Fayl limiti (faqat Telegram sessiyalariga qo‘llanadi; WebApp fotosiga tegmaymiz)
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


# =========================
# PDF helpers (lazy import!)
# =========================
def pdf_split_bytes(pdf_bytes: bytes, range_str: str) -> Optional[bytes]:
    try:
        from PyPDF2 import PdfReader, PdfWriter
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


# =========================
# Resume form handler (WebApp) — rasmga limit qo‘ymadik
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
        return JSONResponse({"status": "error", "error": "resume.docx topilmadi"}, status_code=200)

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
            img_bytes = await photo.read()  # WebApp uchun limit qo‘ymadik
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

    base_name = make_safe_basename(full_name, phone)
    docx_name = f"{base_name}_0.docx"
    pdf_name = f"{base_name}_0.pdf"
    img_name = f"{base_name}{img_ext}"
    json_name = f"{base_name}.json"

    # Guruhga: rasm va JSON
    try:
        if img_bytes:
            await bot.send_document(
                GROUP_CHAT_ID,
                BufferedInputFile(img_bytes, filename=img_name),
                caption=f"🆕 Forma: {full_name}\n📞 {phone}\n👤 TG: {tg_id}"
            )
        payload = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "tg_id": tg_id, "full_name": full_name, "phone": phone,
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
                                caption=f"📄 JSON: {full_name}")
    except Exception as e:
        print("GROUP SEND ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()

    # Mijozga
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

    # WebApp yopish uchun flag
    return {"status": "success", "close": True}


# =========================
# Bot commands
# =========================
@dp.message(Command("start"))
async def start_cmd(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    await m.answer(
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "/new_resume - Yangi obektivka\n"
        "/help - Yordam\n\n"
        "@octagon_print"
    )


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "Asosiy komandalar (Telegram orqali fayl: max 10 MB):\n"
        "/new_resume – Web forma\n"
        "/pdf_split – PDF sahifalarni ajratish\n"
        "/pdf_merge – PDF qo‘shish\n"
        "/pagenum – Sahifa raqami qo‘shish\n"
        "/watermark – Watermark qo‘shish\n"
        "/ocr – Skan PDFdan matn chiqarish\n"
        "/convert – DOCX/PPTX/XLSX/PDF konvertatsiya\n"
        "/translate – PDF matnini tarjima\n"
        "/status – Holat\n"
        "/cancel – Bekor\n"
        "/done – Yakunlash"
    )


@dp.message(Command("new_resume"))
async def new_resume_cmd(m: Message):
    base = (APP_BASE or "").rstrip("/")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Obyektivkani to‘ldirish",
            web_app=WebAppInfo(url=f"{base}/form?id={m.from_user.id}")
        )
    ]])
    await m.answer(
        "👋 Assalomu alaykum!\n📄 Obyektivka (ma’lumotnoma)\n"
        "✅ Tez\n✅ Oson\n✅ Ishonchli\nquyidagi 🌐 web formani to'ldiring\n👇👇👇👇👇👇👇👇👇",
        reply_markup=kb
    )


# ---- Session starters
@dp.message(Command("status"))
async def cmd_status(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("❌ Sessiya yo‘q. /pdf_split, /pdf_merge, /convert … dan boshlang.")
    files_info = "—"
    if s["files"]:
        files_info = "\n".join([f" {i+1}) {f['name']} ({human_size(len(f['bytes']))})"
                                for i, f in enumerate(s["files"])])
    params_info = "Parametrlar hali yo‘q" if not s["params"] else json.dumps(s["params"], ensure_ascii=False)
    await m.answer(
        f"🧩 Jarayon: {s['op']}\n📎 Fayllar: {len(s['files'])}\n{files_info}\n"
        f"⚙️ Parametrlar: {params_info}\nYakunlash: /done | Bekor: /cancel"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    clear_session(m.from_user.id)
    await m.answer("❌ Session bekor qilindi.")


@dp.message(Command("pdf_split"))
async def cmd_split(m: Message):
    new_session(m.from_user.id, "split")
    await m.answer("✂️ PDF Split boshlandi.\n1) PDF yuboring (max 10 MB).\n2) /range 1-3,7\nTugatish: /done | Holat: /status")


@dp.message(Command("pdf_merge"))
async def cmd_merge(m: Message):
    new_session(m.from_user.id, "merge")
    await m.answer("🧷 PDF Merge boshlandi.\nKetma-ket PDF yuboring (har biri max 10 MB).\nTugatish: /done | Holat: /status")


@dp.message(Command("pagenum"))
async def cmd_pagenum(m: Message):
    new_session(m.from_user.id, "pagenum")
    await m.answer("🔢 Sahifa raqami sessiyasi.\n1) PDF yuboring (max 10 MB).\n2) /pos bottom-right\nTugatish: /done | Holat: /status")


@dp.message(Command("watermark"))
async def cmd_watermark(m: Message):
    new_session(m.from_user.id, "watermark")
    await m.answer("💧 Watermark sessiyasi.\n1) PDF yuboring (max 10 MB).\n2) /wm Confidential\n(opsional) /pos bottom-right\nTugatish: /done")


@dp.message(Command("ocr"))
async def cmd_ocr(m: Message):
    new_session(m.from_user.id, "ocr")
    await m.answer("🪄 OCR sessiyasi.\n1) Skan PDF yuboring (max 10 MB).\n2) /lang eng (yoki uzb, rus ...)\nTugatish: /done")


@dp.message(Command("translate"))
async def cmd_translate(m: Message):
    new_session(m.from_user.id, "translate")
    await m.answer("🌐 Tarjima sessiyasi.\n1) PDF yuboring (max 10 MB).\n2) /to uz (maqsad til)\nTugatish: /done")


@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    new_session(m.from_user.id, "convert")
    await m.answer(
        "🔁 Konvert sessiyasi.\n"
        "1) Bitta fayl yuboring (DOCX/PPTX/XLSX/PDF; max 10 MB).\n"
        "2) /target pdf | png | docx | pptx\n"
        "Qoida:\n"
        "• DOCX/PPTX/XLSX → PDF: /target pdf\n"
        "• PPTX → PNG (1-slayd), PDF → PNG (1-sahifa): /target png\n"
        "• PDF → DOCX/PPTX qo‘llanmaydi\n"
        "Tugatish: /done"
    )


# ---- Parametrlar (regex-agnostik)
RE_RANGE  = re.compile(r"^/range\s+(.+)$")
RE_POS    = re.compile(r"^/pos\s+(\S+)$")
RE_WM     = re.compile(r"^/wm\s+(.+)$")
RE_TARGET = re.compile(r"^/target\s+(\S+)$")
RE_LANG   = re.compile(r"^/lang\s+(\S+)$")
RE_TO     = re.compile(r"^/to\s+(\S+)$")
RE_MISS   = re.compile(r"^/(range|pos|wm|target|lang|to)\s*$")


def _get_match(message: Message, data: dict, pattern: re.Pattern):
    mobj = data.get("regexp") or data.get("match")
    if mobj:
        return mobj
    txt = (message.text or "").strip()
    return pattern.match(txt)


@dp.message(F.text.regexp(RE_RANGE))
async def param_range(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "split":
        return await m.answer("Bu parametr /pdf_split sessiyasida.")
    mo = _get_match(m, data, RE_RANGE)
    if not mo:
        return await m.answer("Format: /range 1-3,7")
    s["params"]["range"] = mo.group(1).strip()
    await m.answer("✅ Oraliq qabul qilindi. /status yoki /done")


@dp.message(F.text.regexp(RE_POS))
async def param_pos(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] not in {"pagenum", "watermark"}:
        return await m.answer("Bu parametr /pagenum yoki /watermark sessiyalarida.")
    mo = _get_match(m, data, RE_POS)
    if not mo:
        return await m.answer("Format: /pos bottom-right")
    pos = mo.group(1).strip().lower()
    allowed = {"bottom-right","bottom-left","bottom-center","top-right","top-left","top-center"}
    if pos not in allowed:
        return await m.answer("Ruxsat etilgan: " + ", ".join(sorted(allowed)))
    s["params"]["pos"] = pos
    await m.answer("✅ Pozitsiya qabul qilindi.")


@dp.message(F.text.regexp(RE_WM))
async def param_wm(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "watermark":
        return await m.answer("Bu parametr /watermark sessiyasida.")
    mo = _get_match(m, data, RE_WM)
    if not mo:
        return await m.answer("Format: /wm Confidential")
    text = mo.group(1).strip()
    if not text:
        return await m.answer("Matn bo‘sh bo‘lmasin.")
    s["params"]["wm"] = text[:100]
    await m.answer("✅ Watermark matni qabul qilindi.")


@dp.message(F.text.regexp(RE_TARGET))
async def param_target(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "convert":
        return await m.answer("Bu parametr /convert sessiyasida.")
    mo = _get_match(m, data, RE_TARGET)
    if not mo:
        return await m.answer("Format: /target pdf|png|docx|pptx")
    target = mo.group(1).strip().lower()
    if target not in {"pdf","png","docx","pptx"}:
        return await m.answer("Maqsad format: pdf | png | docx | pptx")
    s["params"]["target"] = target
    await m.answer("✅ Maqsad format qabul qilindi.")


@dp.message(F.text.regexp(RE_LANG))
async def param_lang(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "ocr":
        return await m.answer("Bu parametr /ocr sessiyasida.")
    mo = _get_match(m, data, RE_LANG)
    if not mo:
        return await m.answer("Format: /lang eng")
    s["params"]["lang"] = mo.group(1).strip()
    await m.answer("✅ Til qabul qilindi.")


@dp.message(F.text.regexp(RE_TO))
async def param_to(m: Message, **data):
    s = get_session(m.from_user.id)
    if not s or s["op"] != "translate":
        return await m.answer("Bu parametr /translate sessiyasida.")
    mo = _get_match(m, data, RE_TO)
    if not mo:
        return await m.answer("Format: /to uz")
    s["params"]["to"] = mo.group(1).strip()
    await m.answer("✅ Maqsad til qabul qilindi.")


@dp.message(F.text.regexp(RE_MISS))
async def param_missing(m: Message, **data):
    mo = _get_match(m, data, RE_MISS)
    cmd = mo.group(1) if mo else ""
    tips = {
        "range":  "Masalan: /range 1-3,7",
        "pos":    "Masalan: /pos bottom-right",
        "wm":     "Masalan: /wm Confidential",
        "target": "Masalan: /target pdf|png|docx|pptx",
        "lang":   "Masalan: /lang eng",
        "to":     "Masalan: /to uz",
    }
    await m.answer(f"Parametr yetishmayapti. {tips.get(cmd, '')}")


# ---- Photo’ni bloklash (ixtiyoriy, lekin foydali)
@dp.message(F.photo)
async def reject_photo(m: Message):
    await m.reply("🖼 Rasmni **Document (File)** sifatida yuboring. (Telegram orqali fayl limiti: 10 MB)")


# ---- Fayl qabul qilish (LIMIT bilan)
@dp.message(F.document)
async def collect_file(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return

    # LIMIT: yuklab OLMASDAN avval tekshiramiz
    size_bytes = m.document.file_size or 0
    if size_bytes > MAX_FILE_SIZE:
        clear_session(m.from_user.id)
        mb = size_bytes / (1024 * 1024)
        return await m.reply(
            f"❌ Fayl juda katta: {mb:.1f} MB. Maksimum {MAX_FILE_MB} MB.\n"
            f"Jarayon bekor qilindi. Kichikroq fayl bilan qayta boshlang."
        )

    name = m.document.file_name or "file.bin"
    mime = m.document.mime_type or "application/octet-stream"

    # Endi xavfsiz yuklab olamiz
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
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi.")
        s["files"].append({"name": name, "bytes": data, "mime": mime})
        return await m.reply(f"Qo‘shildi ✅  ({name})  — jami: {len(s['files'])}")

    if op in {"split", "pagenum", "watermark", "ocr", "translate", "convert"}:
        s["files"] = [{"name": name, "bytes": data, "mime": mime}]
        if op in {"split", "pagenum", "watermark", "ocr", "translate"} and mime != "application/pdf":
            return await m.reply("Bu sessiyada faqat PDF qabul qilinadi.")
        await m.reply(
            f"Fayl qabul qilindi: {name} ({human_size(len(data))}) ✅\n"
            "(/status yoki parametr yuboring, keyin /done)"
        )


# ---- /done
@dp.message(Command("done"))
async def cmd_done(m: Message):
    s = get_session(m.from_user.id)
    if not s:
        return await m.answer("Sessiya yo‘q.")

    op = s["op"]; files = s["files"]; p = s["params"]

    try:
        if op == "split":
            if not files: return await m.answer("PDF yuboring.")
            if "range" not in p: return await m.answer("Oraliq kerak: /range 1-3,7")
            out = pdf_split_bytes(files[0]["bytes"], p["range"])
            if not out: return await m.answer("Ajratishda xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="split.pdf"),
                                    caption="✅ Split tayyor")
            clear_session(m.from_user.id); return

        if op == "merge":
            if len(files) < 2: return await m.answer("Kamida 2 ta PDF yuboring.")
            out = pdf_merge_bytes([f["bytes"] for f in files])
            if not out: return await m.answer("Merge xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="merge.pdf"),
                                    caption="✅ Merge tayyor")
            clear_session(m.from_user.id); return

        if op == "pagenum":
            if not files: return await m.answer("PDF yuboring.")
            pos = p.get("pos", "bottom-right")
            out = pdf_overlay_text(files[0]["bytes"], text="{page}", pos=pos, font_size=10)
            if not out: return await m.answer("Sahifa raqami qo‘shishda xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="pagenum.pdf"),
                                    caption="✅ Sahifa raqamlari qo‘shildi")
            clear_session(m.from_user.id); return

        if op == "watermark":
            if not files: return await m.answer("PDF yuboring.")
            wm = p.get("wm")
            if not wm: return await m.answer("Matn bering: /wm Confidential")
            pos = p.get("pos", "bottom-right")
            out = pdf_overlay_text(files[0]["bytes"], text=wm, pos=pos, font_size=14)
            if not out: return await m.answer("Watermarkda xatolik.")
            await bot.send_document(m.chat.id, BufferedInputFile(out, filename="watermark.pdf"),
                                    caption="✅ Watermark qo‘shildi")
            clear_session(m.from_user.id); return

        if op == "ocr":
            if not files: return await m.answer("PDF yuboring.")
            lang = p.get("lang", "eng")
            txt = ocr_pdf_to_text(files[0]["bytes"], lang=lang)
            if not txt: return await m.answer("OCR natijasi bo‘sh. /lang eng sinab ko‘ring.")
            await bot.send_document(m.chat.id, BufferedInputFile(txt.encode("utf-8"), filename="ocr.txt"),
                                    caption=f"✅ OCR tayyor (lang={lang})")
            clear_session(m.from_user.id); return

        if op == "translate":
            if not files: return await m.answer("PDF yuboring.")
            to = p.get("to", "uz")
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(files[0]["bytes"]))
                src_text = "\n\n".join([pg.extract_text() or "" for pg in reader.pages]).strip()
            except Exception:
                src_text = ""
            if not src_text:
                return await m.answer("PDFdan matn olinmadi. Avval /ocr bilan text oling.")
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
            clear_session(m.from_user.id); return

        if op == "convert":
            if not files: return await m.answer("Fayl yuboring.")
            target = p.get("target")
            if target not in {"pdf","png","docx","pptx"}:
                return await m.answer("Maqsad format: /target pdf|png|docx|pptx")

            name = files[0]["name"].lower()
            data = files[0]["bytes"]

            if target == "pdf" and name.endswith((".docx",".pptx",".xlsx")):
                out = libre_convert(data, "pdf", in_name=name)
                if not out: return await m.answer("Konvert xatolik.")
                await bot.send_document(m.chat.id,
                    BufferedInputFile(out, filename=f"{os.path.splitext(name)[0]}.pdf"),
                    caption="✅ PDF tayyor")
                clear_session(m.from_user.id); return

            if target == "png" and (name.endswith(".pptx") or name.endswith(".pdf")):
                try:
                    from pdf2image import convert_from_bytes
                    if name.endswith(".pptx"):
                        pdf = libre_convert(data, "pdf", in_name=name)
                        if not pdf: return await m.answer("PPTX->PDF xatolik.")
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
                    clear_session(m.from_user.id); return
                except Exception as e:
                    print("PNG CONVERT ERROR:", repr(e), file=sys.stderr)
                    return await m.answer("PNG konvert xatolik (poppler/tesseract o‘rnatilganini tekshiring).")

            return await m.answer("Bu yo‘nalish hozircha qo‘llanmaydi.")
    except Exception as e:
        print("DONE ERROR:", repr(e), file=sys.stderr)
        traceback.print_exc()
        await m.answer("❌ Jarayon davomida xatolik. /status yoki /cancel.")
        return


# ---- Commands list (menu)
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
    await bot.set_my_commands(cmds)
    print("✅ Bot commands list yangilandi")


@app.on_event("startup")
async def on_startup():
    try:
        await _set_commands()
    except Exception as e:
        print("SET COMMANDS ERROR:", repr(e), file=sys.stderr)


# ---- Webhook
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


# ---- Debug
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}


@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}
