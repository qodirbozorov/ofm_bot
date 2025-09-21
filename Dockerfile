FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps: LibreOffice + OCR + Poppler + fonts (yengil to‘plam)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress libreoffice-calc \
    poppler-utils \
    tesseract-ocr \
    fontconfig fonts-dejavu-core \
 && fc-cache -f -v || true \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway PORT ni o‘zi beradi; 8080 — lokalda fallback
EXPOSE 8080

# Muhim: Railway PORT muhit o‘zgaruvchisini ishlatish
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
