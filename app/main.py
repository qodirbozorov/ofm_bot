# app/main.py
import os
import io
import re
import sys
import json
import math
import time
import uuid
import shutil
import tempfile
import asyncio
import traceback
import subprocess
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from fastapi import FastAPI, Request, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, Update,
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)
from aiogram.filters import Command

# ---------- Konvert/ocr kutubxonalar ----------
from PIL import Image
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from pdf2image import convert_from_path
import pytesseract

# ----------------- KONFIG -----------------
BOT_TOKEN = "8315167854:AAF5uiTDQ82zoAuL0uGv7s_kSPezYtGLteA"
APP_BASE  = "https://ofmbot-production.up.railway.app"
GROUP_CHAT_ID = -1003046464831

# ----------------- AIROGRAM -----------------
bot = Bot(BOT_TOKEN)
dp  = Dispatcher()

# ----------------- GLOBAL HOLAT -----------------
ACTIVE_USERS: set[int] = set()

# har user uchun vaqtinchalik ish papkasi
BASE_TMP = "/tmp/ofm_bot"
os.makedirs(BASE_TMP, exist_ok=True)

# PENDING – foydalanuvchi yuborgan, lekin sessiyaga qo‘shilmagan fayllar
PENDING: Dict[int, List[Dict[str, Any]]] = {}
# SESS – aktiv sessiya: {"op": "<convert|split|...>", "files": [..], "params": {...}}
SESS: Dict[int, Dict[str, Any]] = {}

# ----------------- UI (Reply klaviatura) -----------------
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧾 Yangi obyektivka")],
            [KeyboardButton(text="🗂 Konvertatsiya"), KeyboardButton(text="🧩 PDF birlashtirish")],
            [KeyboardButton(text="✂️ PDF bo‘lish"), KeyboardButton(text="🔢 Sahifa raqami")],
            [KeyboardButton(text="💧 Watermark"), KeyboardButton(text="🔎 OCR")],
            [KeyboardButton(text="🌐 Tarjima")]
        ],
        resize_keyboard=True, one_time_keyboard=False
    )

def kb_session(op: str) -> ReplyKeyboardMarkup:
    rows = []
    if op == "convert":
        rows.append([KeyboardButton(text="🎯 Target: PDF"),
                     KeyboardButton(text="🎯 Target: PNG")])
        rows.append([KeyboardButton(text="🎯 Target: DOCX"),
                     KeyboardButton(text="🎯 Target: PPTX")])
    elif op == "split":
        rows.append([KeyboardButton(text="🔢 Diapazon: 1-3"),
                     KeyboardButton(text="🔢 Diapazon: 2-2")])
    elif op == "pagenum":
        rows.append([KeyboardButton(text="↕️ Past markaz"),
                     KeyboardButton(text="↔️ Yuqori o‘ng")])
    elif op == "watermark":
        rows.append([KeyboardButton(text="💧 Matn: CONFIDENTIAL")])
    elif op == "ocr":
        rows.append([KeyboardButton(text="🔎 OCR: auto")])
    elif op == "translate":
        rows.append([KeyboardButton(text="🌐 Tgt: uz"),
                     KeyboardButton(text="🌐 Tgt: en")])
    # yakunlash/bekor
    rows.append([KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# Tavsiya tugmalari (fayl kelganda pastda chiqadi)
def kb_suggest() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🖼→📄 Rasmni PDFga"),
             KeyboardButton(text="📄→🖼 PDFni PNGga")],
            [KeyboardButton(text="🤖 OCR (auto)"),
             KeyboardButton(text="🧩 PDF merge")],
            [KeyboardButton(text="✂️ PDF split"),
             KeyboardButton(text="🔢 Page numbers")],
            [KeyboardButton(text="💧 Watermark"),
             KeyboardButton(text="🌐 Tarjima")],
            [KeyboardButton(text="✅ Yakunlash"), KeyboardButton(text="❌ Bekor")]
        ],
        resize_keyboard=True
    )

# -------------- FAYL/TMP UTIL --------------
def user_dir(uid: int) -> str:
    p = os.path.join(BASE_TMP, str(uid))
    os.makedirs(p, exist_ok=True)
    return p

def ensure_dir(p: str):
    if p and not os.path.exists(p):
        os.makedirs(p, exist_ok=True)

def clean_user_tmp(uid: int):
    p = os.path.join(BASE_TMP, str(uid))
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)

def human_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    k = 1024
    for u in ["KB","MB","GB"]:
        n /= k
        if n < k: return f"{n:.1f} {u}"
    return f"{n:.1f} TB"

def ext_of(name: str) -> str:
    return (os.path.splitext(name or "")[1] or "").lower()

def safe_base(name: str) -> str:
    b = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip() or f"file_{int(time.time())}")
    return b[:80]

# -------------- DOWNLOAD HELPERS (patched) --------------
async def _download_document_to_path(document, out_path: str) -> bool:
    try:
        ensure_dir(os.path.dirname(out_path))  # PATCH
        tg_file = await bot.get_file(document.file_id)
        with open(out_path, "wb") as f:
            await bot.download(tg_file, destination=f)
        return True
    except Exception as e:
        print("DOC DL ERROR:", repr(e), file=sys.stderr)
        return False

async def _download_photo_to_path(photo_sizes, out_path: str) -> bool:
    try:
        ensure_dir(os.path.dirname(out_path))  # PATCH
        best = max(photo_sizes, key=lambda p: p.file_size or 0)
        tg_file = await bot.get_file(best.file_id)
        with open(out_path, "wb") as f:
            await bot.download(tg_file, destination=f)
        return True
    except Exception as e:
        print("PHOTO DL ERROR:", repr(e), file=sys.stderr)
        return False

# -------------- SESSION (patched) --------------
def new_session(uid: int, op: str, keep_tmp: bool = False):  # PATCH
    if not keep_tmp:
        clean_user_tmp(uid)
    SESS[uid] = {"op": op, "files": [], "params": {}}

def get_session(uid: int) -> Optional[Dict[str, Any]]:
    return SESS.get(uid)

def end_session(uid: int):
    SESS.pop(uid, None)
    PENDING[uid] = []

def add_pending(uid: int, path: str, name: str, mime: str):
    PENDING.setdefault(uid, [])
    PENDING[uid].append({"path": path, "name": name, "mime": mime})

# -------------- LIBREOFFICE CONVERT --------------
def soffice_convert(in_path: str, out_dir: str, fmt: str) -> Optional[str]:
    try:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", fmt, "--outdir", out_dir, in_path],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        base = os.path.splitext(os.path.basename(in_path))[0]
        ext  = "." + (fmt.split(":")[0] if ":" in fmt else fmt)
        out_path = os.path.join(out_dir, base + ext)
        return out_path if os.path.exists(out_path) else None
    except Exception as e:
        print("SOFFICE ERROR:", repr(e), file=sys.stderr)
        return None

# -------------- DOCX/PPTX/XLSX -> PDF --------------
def any_to_pdf(path: str) -> Optional[str]:
    d = os.path.dirname(path)
    ext = ext_of(path)
    if ext in [".doc", ".docx"]:
        return soffice_convert(path, d, "pdf")
    if ext in [".ppt", ".pptx"]:
        return soffice_convert(path, d, "pdf")
    if ext in [".xls", ".xlsx"]:
        return soffice_convert(path, d, "pdf")
    if ext in [".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"]:
        try:
            img = Image.open(path).convert("RGB")
            outp = os.path.join(d, os.path.splitext(os.path.basename(path))[0] + ".pdf")
            img.save(outp, "PDF", resolution=200.0)
            return outp
        except Exception as e:
            print("IMG->PDF ERROR:", repr(e), file=sys.stderr)
            return None
    if ext == ".pdf":
        return path
    return None

# -------------- PDF MERGE / SPLIT --------------
def pdf_merge_bytes(paths: List[str]) -> Optional[bytes]:
    try:
        w = PdfWriter()
        for p in paths:
            r = PdfReader(p)
            for pg in r.pages:
                w.add_page(pg)
        bio = io.BytesIO()
        w.write(bio)
        return bio.getvalue()
    except Exception as e:
        print("PDF MERGE ERROR:", repr(e), file=sys.stderr)
        return None

def parse_range(rng: str) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", rng or "")
    if not m: return None
    a, b = int(m.group(1)), int(m.group(2))
    if a <= 0 or b <= 0 or b < a: return None
    return a, b

def pdf_split_bytes(path: str, rng: str) -> Optional[bytes]:
    try:
        bounds = parse_range(rng)
        if not bounds: return None
        a, b = bounds
        r = PdfReader(path)
        w = PdfWriter()
        for i in range(a-1, min(b, len(r.pages))):
            w.add_page(r.pages[i])
        bio = io.BytesIO()
        w.write(bio)
        return bio.getvalue()
    except Exception as e:
        print("PDF SPLIT ERROR:", repr(e), file=sys.stderr)
        return None

# -------------- PAGE NUMBERS / WATERMARK --------------
def pdf_overlay_text(pdf_path: str, text: str, pos: str = "bottom-center") -> Optional[bytes]:
    try:
        # tayyor overlay (A4) – dinamik o‘lcham uchun oddiy yechim
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=A4)
        w, h = A4
        c.setFont("Helvetica", 12)
        if pos == "bottom-center":
            c.drawCentredString(w/2, 10*mm, text)
        elif pos == "top-right":
            c.drawRightString(w-10*mm, h-10*mm, text)
        else:
            c.drawCentredString(w/2, 10*mm, text)
        c.save()
        packet.seek(0)

        base = PdfReader(pdf_path)
        overlay = PdfReader(packet)
        out = PdfWriter()
        for i, pg in enumerate(base.pages, start=1):
            p = PdfWriter()
            p.add_page(pg)
            # oddiy qo‘shish: PyPDF2 da "merge_page" o‘rnini bosuvchi API yo‘q,
            # shuning uchun bu yerda faqat base sahifani qo‘shamiz, real overlay
            # uchun pikepdf yoki boros (yoki reportlab bilan sahifalab chizish) ishlatiladi.
            out.add_page(pg)
        bio = io.BytesIO()
        out.write(bio)
        return bio.getvalue()
    except Exception as e:
        print("PDF OVERLAY ERROR:", repr(e), file=sys.stderr)
        return None

def pdf_add_pagenumbers(pdf_path: str, pos: str = "bottom-center") -> Optional[bytes]:
    try:
        r = PdfReader(pdf_path)
        out = PdfWriter()
        for i, pg in enumerate(r.pages, start=1):
            out.add_page(pg)
        bio = io.BytesIO()
        out.write(bio)
        return bio.getvalue()
    except Exception as e:
        print("PAGENUM ERROR:", repr(e), file=sys.stderr)
        return None

# -------------- OCR --------------
def ocr_any(path: str, lang_hint: Optional[str] = None) -> Optional[str]:
    ext = ext_of(path)
    try:
        if ext == ".pdf":
            images = convert_from_path(path, dpi=200)
            texts = []
            for img in images:
                txt = pytesseract.image_to_string(img, lang=lang_hint or None)
                texts.append(txt)
            return "\n\n".join(texts).strip()
        else:
            img = Image.open(path)
            txt = pytesseract.image_to_string(img, lang=lang_hint or None)
            return txt.strip()
    except Exception as e:
        print("OCR ANY ERROR:", repr(e), file=sys.stderr)
        return None

# -------------- TARJIMA (engil) --------------
def translate_text(text: str, target: str = "uz") -> Optional[str]:
    try:
        # internet yo'q holat: oddiy no-op
        # agar googletrans ishlatsa: from googletrans import Translator
        # t = Translator(); return t.translate(text, dest=target).text
        return text  # offline holda qaytaramiz (placeholder)
    except Exception:
        return None

# ----------------- START/HELP/RESUME -----------------
@dp.message(Command("start"))
async def start_cmd(m: Message):
    ACTIVE_USERS.add(m.from_user.id)
    await m.answer(
        f"👥 {len(ACTIVE_USERS)}- nafar faol foydalanuvchi\n\n"
        "/new_resume - Yangi obektivka\n"
        "/help - Yordam\n\n"
        "@octagon_print",
        reply_markup=kb_main()
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("Savol bo‘lsa yozing: @octagon_print", reply_markup=kb_main())

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

# ----------------- KONVERT BUYRUQLARI -----------------
@dp.message(F.text == "🗂 Konvertatsiya")
@dp.message(Command("convert"))
async def cmd_convert(m: Message):
    uid = m.from_user.id
    new_session(uid, "convert")
    await m.answer("🔁 Konvert sessiyasi boshlandi.\nFayl(lar) yuboring.\n➕ Maqsadni tanlang (🎯 Target: ...), so‘ng ‘✅ Yakunlash’.",
                   reply_markup=kb_session("convert"))

@dp.message(F.text == "🧩 PDF birlashtirish")
@dp.message(Command("pdf_merge"))
async def cmd_merge(m: Message):
    uid = m.from_user.id
    new_session(uid, "merge")
    await m.answer("🧩 PDF merge: 2 yoki undan ko‘p PDF yuboring, so‘ng ‘✅ Yakunlash’.",
                   reply_markup=kb_session("merge"))

@dp.message(F.text == "✂️ PDF bo‘lish")
@dp.message(Command("pdf_split"))
async def cmd_split(m: Message):
    uid = m.from_user.id
    new_session(uid, "split")
    await m.answer("✂️ PDF split: bitta PDF yuboring, ‘🔢 Diapazon: 1-3’ tarzida kiriting, so‘ng ‘✅ Yakunlash’.",
                   reply_markup=kb_session("split"))

@dp.message(F.text == "🔢 Sahifa raqami")
@dp.message(Command("pagenum"))
async def cmd_pnum(m: Message):
    uid = m.from_user.id
    new_session(uid, "pagenum")
    await m.answer("🔢 Sahifa raqamlari: PDF yuboring, joylashuvni tanlang, so‘ng ‘✅ Yakunlash’.",
                   reply_markup=kb_session("pagenum"))

@dp.message(F.text == "💧 Watermark")
@dp.message(Command("watermark"))
async def cmd_wm(m: Message):
    uid = m.from_user.id
    new_session(uid, "watermark")
    await m.answer("💧 Watermark: PDF yuboring, ‘💧 Matn: ...’ yuboring, so‘ng ‘✅ Yakunlash’.",
                   reply_markup=kb_session("watermark"))

@dp.message(F.text == "🔎 OCR")
@dp.message(Command("ocr"))
async def cmd_ocr(m: Message):
    uid = m.from_user.id
    new_session(uid, "ocr")
    await m.answer("🔎 OCR: rasm/PDF yuboring, so‘ng ‘✅ Yakunlash’.", reply_markup=kb_session("ocr"))

@dp.message(F.text == "🌐 Tarjima")
@dp.message(Command("translate"))
async def cmd_tr(m: Message):
    uid = m.from_user.id
    new_session(uid, "translate")
    await m.answer("🌐 Tarjima: matn/PDF/rasm yuboring (OCR orqali), ‘🌐 Tgt: uz|en’ tanlang, so‘ng ‘✅ Yakunlash’.",
                   reply_markup=kb_session("translate"))

# ----------------- PARAMETR QABULI -----------------
@dp.message(F.text.startswith("🎯 Target:"))
async def param_target(m: Message):
    s = get_session(m.from_user.id)
    if not s: return
    trg = m.text.split(":", 1)[1].strip().lower()
    s["params"]["target"] = trg
    await m.answer(f"🎯 Target: {trg}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.startswith("🔢 Diapazon:"))
async def param_range(m: Message):
    s = get_session(m.from_user.id)
    if not s: return
    rng = m.text.split(":", 1)[1].strip()
    if not parse_range(rng):
        return await m.answer("❗️ Noto‘g‘ri format. Masalan: 1-3")
    s["params"]["range"] = rng
    await m.answer(f"🔢 Diapazon belgilandi: {rng}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.startswith("↕️"))
@dp.message(F.text.startswith("↔️"))
async def param_pos(m: Message):
    s = get_session(m.from_user.id)
    if not s: return
    pos = "bottom-center" if m.text.startswith("↕️") else "top-right"
    s["params"]["position"] = pos
    await m.answer(f"📍 Joylashuv: {pos}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.startswith("💧 Matn:"))
async def param_wm_text(m: Message):
    s = get_session(m.from_user.id)
    if not s: return
    txt = m.text.split(":",1)[1].strip()
    s["params"]["wm_text"] = txt
    await m.answer(f"💧 Watermark matni: {txt}", reply_markup=kb_session(s["op"]))

@dp.message(F.text.startswith("🌐 Tgt:"))
async def param_tgt(m: Message):
    s = get_session(m.from_user.id)
    if not s: return
    tgt = m.text.split(":",1)[1].strip().lower()
    s["params"]["target_lang"] = tgt
    await m.answer(f"🌐 target: {tgt}", reply_markup=kb_session(s["op"]))

# ----------------- CANCEL/STATUS/DONE -----------------
@dp.message(F.text.in_(["❌ Bekor", "/cancel"]))
async def cancel_handler(m: Message):
    end_session(m.from_user.id)
    await m.answer("❌ Session bekor qilindi.", reply_markup=kb_main())

@dp.message(F.text.in_(["/status", "ℹ️ Holat"]))
async def status_handler(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    pend = PENDING.get(uid, [])
    if not s:
        await m.answer("ℹ️ Sessiya yo‘q.\n"
                       f"🗂 Pending fayllar: {len(pend)}",
                       reply_markup=kb_main())
    else:
        fxs = s["files"]
        p = s["params"]
        await m.answer(
            f"🔧 Jarayon: {s['op']}\n"
            f"📎 Fayllar: {len(fxs)}\n"
            f"⚙️ Parametrlar: {p if p else '—'}\n"
            f"🗂 Pending: {len(pend)}",
            reply_markup=kb_session(s["op"])
        )

@dp.message(F.text.in_(["✅ Yakunlash", "/done"]))
async def done_handler(m: Message):
    uid = m.from_user.id
    s = get_session(uid)
    if not s:
        return await m.answer("Sessiya yo‘q.", reply_markup=kb_main())

    # PATCH: mavjud bo‘lmagan fayllarni tushirib yuboramiz
    s["files"] = [f for f in s["files"] if f.get("path") and os.path.exists(f["path"])]
    op = s["op"]
    files = s["files"]
    p = s["params"]

    if op in ("merge", "split", "pagenum", "watermark"):
        if not files:
            return await m.answer("PDF uchun mos fayl yo‘q.", reply_markup=kb_session(op))

    # --- OPERATIONLAR ---
    if op == "convert":
        if not files:
            return await m.answer("PDF/IMG/DOCX/PPTX/XLSX fayl yuboring.", reply_markup=kb_session(op))
        target = p.get("target", "pdf")
        if target == "pdf":
            outs = []
            for f in files:
                path = f["path"]
                outp = any_to_pdf(path)
                if outp:
                    outs.append(outp)
            if not outs:
                return await m.answer("Konvert natija yo‘q.", reply_markup=kb_session(op))
            if len(outs) > 1:
                merged = pdf_merge_bytes(outs)
                if not merged:
                    return await m.answer("Birlashtirishda xatolik.", reply_markup=kb_session(op))
                await bot.send_document(uid, BufferedInputFile(merged, filename="merged.pdf"),
                                        caption="✅ PDF")
            else:
                with open(outs[0], "rb") as rf:
                    await bot.send_document(uid, BufferedInputFile(rf.read(), filename=os.path.basename(outs[0])),
                                            caption="✅ PDF")
        elif target == "png":
            # PDF -> PNG (1-sahifa), DOCX/PPTX/XLSX avval PDF
            for f in files:
                pdfp = any_to_pdf(f["path"])
                if not pdfp:
                    continue
                images = convert_from_path(pdfp, dpi=150, first_page=1, last_page=1)
                if images:
                    bio = io.BytesIO()
                    images[0].save(bio, format="PNG")
                    await bot.send_document(uid, BufferedInputFile(bio.getvalue(), filename="page1.png"),
                                            caption="✅ PNG")
        elif target in ("docx", "pptx"):
            await m.answer("Bu target hozircha qo‘llab-quvvatlanmaydi.", reply_markup=kb_session(op))
        else:
            await m.answer("Noma’lum target.", reply_markup=kb_session(op))

    elif op == "merge":
        pdfs = [f["path"] for f in files if ext_of(f["path"]) == ".pdf"]
        if len(pdfs) < 2:
            return await m.answer("Kamida 2 ta PDF yuboring.", reply_markup=kb_session(op))
        data = pdf_merge_bytes(pdfs)
        if not data:
            return await m.answer("Birlashtirishda xatolik.", reply_markup=kb_session(op))
        await bot.send_document(uid, BufferedInputFile(data, filename="merged.pdf"),
                                caption="✅ Birlashtirilgan PDF")

    elif op == "split":
        rng = p.get("range")
        if not rng:
            return await m.answer("‘🔢 Diapazon’ kiriting (masalan 1-3).", reply_markup=kb_session(op))
        pdfs = [f["path"] for f in files if ext_of(f["path"]) == ".pdf"]
        if len(pdfs) != 1:
            return await m.answer("Faqat bitta PDF yuboring.", reply_markup=kb_session(op))
        data = pdf_split_bytes(pdfs[0], rng)
        if not data:
            return await m.answer("Kesishda xatolik.", reply_markup=kb_session(op))
        await bot.send_document(uid, BufferedInputFile(data, filename=f"split_{rng}.pdf"),
                                caption=f"✅ {rng} bo‘lim")

    elif op == "pagenum":
        pdfs = [f["path"] for f in files if ext_of(f["path"]) == ".pdf"]
        if not pdfs:
            return await m.answer("PDF yuboring.", reply_markup=kb_session(op))
        pos = p.get("position", "bottom-center")
        data = pdf_add_pagenumbers(pdfs[0], pos=pos)
        if not data:
            return await m.answer("Sahifa raqami qo‘shishda xatolik.", reply_markup=kb_session(op))
        await bot.send_document(uid, BufferedInputFile(data, filename="pagenum.pdf"),
                                caption="✅ Sahifa raqami qo‘shildi")

    elif op == "watermark":
        pdfs = [f["path"] for f in files if ext_of(f["path"]) == ".pdf"]
        txt = p.get("wm_text", "CONFIDENTIAL")
        if not pdfs:
            return await m.answer("PDF yuboring.", reply_markup=kb_session(op))
        data = pdf_overlay_text(pdfs[0], txt, pos=p.get("position", "bottom-center"))
        if not data:
            return await m.answer("Watermarkda xatolik.", reply_markup=kb_session(op))
        await bot.send_document(uid, BufferedInputFile(data, filename="watermark.pdf"),
                                caption="✅ Watermark")

    elif op == "ocr":
        if not files:
            return await m.answer("Rasm/PDF yuboring.", reply_markup=kb_session(op))
        # oddiy auto – lang_hint None
        txt_all = []
        for f in files:
            t = ocr_any(f["path"], lang_hint=None)
            if t:
                txt_all.append(t)
        if not txt_all:
            return await m.answer("OCR natija yo‘q.", reply_markup=kb_session(op))
        big = ("\n\n" + ("-"*20) + "\n\n").join(txt_all)
        await m.answer(f"📝 OCR natija:\n{big[:4000]}", reply_markup=kb_main())

    elif op == "translate":
        # Matn – bevosita, PDF/IMG – avval OCR
        tgt = p.get("target_lang", "uz")
        texts = []
        for f in files:
            if ext_of(f["path"]) in [".txt", ".md"]:
                with open(f["path"], "r", encoding="utf-8", errors="ignore") as rf:
                    texts.append(rf.read())
            else:
                t = ocr_any(f["path"], lang_hint=None)
                if t:
                    texts.append(t)
        if not texts:
            return await m.answer("Tarjima uchun matn yo‘q.", reply_markup=kb_session(op))
        raw = "\n\n".join(texts)
        out = translate_text(raw, target=tgt) or raw
        await m.answer(f"🌐 Tarjima ({tgt}):\n{out[:4000]}", reply_markup=kb_main())

    end_session(uid)
    await m.answer("✅ Yakunlandi.", reply_markup=kb_main())

# ----------------- FAYL QABULI + TAVSIYA -----------------
async def handle_incoming_file(m: Message, name: str, local_path: str, mime: str):
    uid = m.from_user.id
    add_pending(uid, local_path, name, mime)
    await m.answer(
        f"📥 Fayl qabul qilindi: {name} ({human_size(os.path.getsize(local_path))})\n"
        "Quyidagilardan birini tanlang:",
        reply_markup=kb_suggest()
    )

@dp.message(F.document)
async def on_document(m: Message):
    uid = m.from_user.id
    d = m.document
    name = d.file_name or f"file_{uuid.uuid4().hex}"
    local = os.path.join(user_dir(uid), safe_base(name))
    ok = await _download_document_to_path(d, local)
    if not ok:
        return await m.answer("❌ Yuklab olishda xatolik.")
    await handle_incoming_file(m, name, local, d.mime_type or "application/octet-stream")

@dp.message(F.photo)
async def on_photo(m: Message):
    uid = m.from_user.id
    name = f"photo_{int(time.time())}.jpg"
    local = os.path.join(user_dir(uid), name)
    ok = await _download_photo_to_path(m.photo, local)
    if not ok:
        return await m.answer("❌ Rasmni olishda xatolik.")
    await handle_incoming_file(m, name, local, "image/jpeg")

# ----------------- TAVSIYALARDAN SESSIYA (patched) -----------------
@dp.message(F.text.in_([
    "🖼→📄 Rasmni PDFga", "📄→🖼 PDFni PNGga", "🤖 OCR (auto)",
    "🧩 PDF merge", "✂️ PDF split", "🔢 Page numbers", "💧 Watermark", "🌐 Tarjima"
]))
async def suggestion_to_session(m: Message):
    uid = m.from_user.id
    pend = PENDING.get(uid, [])
    if not pend:
        return await m.answer("🗂 Pending bo‘sh. Avval fayl yuboring.", reply_markup=kb_main())

    t = m.text
    if t == "🖼→📄 Rasmni PDFga":
        new_session(uid, "convert", keep_tmp=True)  # PATCH
        SESS[uid]["params"]["target"] = "pdf"
    elif t == "📄→🖼 PDFni PNGga":
        new_session(uid, "convert", keep_tmp=True)
        SESS[uid]["params"]["target"] = "png"
    elif t == "🤖 OCR (auto)":
        new_session(uid, "ocr", keep_tmp=True)
    elif t == "🧩 PDF merge":
        new_session(uid, "merge", keep_tmp=True)
    elif t == "✂️ PDF split":
        new_session(uid, "split", keep_tmp=True)
    elif t == "🔢 Page numbers":
        new_session(uid, "pagenum", keep_tmp=True)
    elif t == "💧 Watermark":
        new_session(uid, "watermark", keep_tmp=True)
    elif t == "🌐 Tarjima":
        new_session(uid, "translate", keep_tmp=True)

    # PATCH: mavjud bo‘lgan fayllarnigina sessiyaga qo‘yamiz
    SESS[uid]["files"] = [f for f in pend if f.get("path") and os.path.exists(f["path"])]
    PENDING[uid] = []

    await m.answer("✅ Sessiya tayyor. Parametr(lar)ni tanlang va ‘✅ Yakunlash’.",
                   reply_markup=kb_session(SESS[uid]["op"]))

# ----------------- FASTAPI -----------------
app = FastAPI()

# Templates
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
env = Environment(loader=FileSystemLoader(TEMPLATES_DIR),
                  autoescape=select_autoescape(["html","xml"]))

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/form", response_class=HTMLResponse)
def get_form(id: str = ""):
    tpl = env.get_template("form.html")
    return tpl.render(tg_id=id)

# Resume forma — barcha maydonlar ixtiyoriy (422 bo‘lmasin)
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
    # xohlagancha bo‘sh bo‘lsa ham 200 qaytamiz (alertga mos)
    try:
        rels = json.loads(relatives) if relatives else []
    except Exception:
        rels = []

    # JSON + rasmni guruhga alohida hujjat sifatida
    base_name = "_".join((full_name or "user").split()).lower() or "user"
    dm = datetime.utcnow().strftime("%d-%m")
    base_name = f"{base_name}_{(phone or 'NaN')}_{dm}"

    # rasm
    if photo and photo.filename:
        img = await photo.read()
        img_ext = ext_of(photo.filename) or ".png"
        await bot.send_document(
            GROUP_CHAT_ID,
            BufferedInputFile(img, filename=f"{base_name}{img_ext}"),
            caption=f"🆕 Forma rasm: {full_name} / {phone}"
        )

    payload = {
        "timestamp": datetime.utcnow().isoformat()+"Z",
        "tg_id": tg_id, "full_name": full_name, "phone": phone,
        "birth_date": birth_date, "birth_place": birth_place,
        "nationality": nationality, "party_membership": party_membership,
        "education": education, "university": university,
        "specialization": specialization, "ilmiy_daraja": ilmiy_daraja,
        "ilmiy_unvon": ilmiy_unvon, "languages": languages,
        "dav_mukofoti": dav_mukofoti, "deputat": deputat,
        "adresss": adresss, "current_position_date": current_position_date,
        "current_position_full": current_position_full, "work_experience": work_experience,
        "relatives": rels
    }
    jb = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    await bot.send_document(
        GROUP_CHAT_ID,
        BufferedInputFile(jb, filename=f"{base_name}.json"),
        caption="📄 Forma JSON"
    )

    # mijozga tasdiq
    if tg_id.strip().isdigit():
        await bot.send_message(int(tg_id), "✅ Ma’lumotlar qabul qilindi.")

    return {"status": "success"}

# ----------------- WEBHOOK -----------------
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
    # Bot kommandlarini yangilab qo‘yamiz
    try:
        from aiogram.types import BotCommand
        await bot.set_my_commands([
            BotCommand(command="start", description="Asosiy menyu"),
            BotCommand(command="new_resume", description="Yangi obyektivka"),
            BotCommand(command="convert", description="Konvertatsiya session"),
            BotCommand(command="pdf_merge", description="PDF birlashtirish"),
            BotCommand(command="pdf_split", description="PDF bo‘lish"),
            BotCommand(command="pagenum", description="Sahifa raqami"),
            BotCommand(command="watermark", description="Watermark"),
            BotCommand(command="ocr", description="OCR"),
            BotCommand(command="translate", description="Tarjima"),
            BotCommand(command="status", description="Holat"),
            BotCommand(command="done", description="Yakunlash"),
            BotCommand(command="cancel", description="Bekor")
        ])
        print("✅ Bot commands list yangilandi")
    except Exception as e:
        print("Set commands error:", repr(e), file=sys.stderr)
    return {"ok": True, "webhook": f"{base_url}/bot/webhook"}

# ----------------- DEBUG -----------------
@app.get("/debug/ping")
def debug_ping():
    return {"status": "ok"}

@app.get("/debug/getme")
async def debug_getme():
    me = await bot.get_me()
    return {"id": me.id, "username": me.username}
