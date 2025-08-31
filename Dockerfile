FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# System deps: LibreOffice + OCR + Poppler + fonts (eng yengil to'plam)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-common libreoffice-writer libreoffice-impress libreoffice-calc \
    poppler-utils \            # pdf2image (pdftoppm) uchun
    tesseract-ocr \            # OCR dvigatel
    fontconfig fonts-dejavu-core \
 && fc-cache -f -v || true \
 && rm -rf /var/lib/apt/lists/*
# --- OPTIONAL (Times New Roman) ---
# Agar PDF’da Times New Roman almashib ketaversa, pastdagini ochib qo‘ying.
RUN set -eux; \
  sed -i 's/main/main contrib non-free non-free-firmware/g' /etc/apt/sources.list; \
  apt-get update; \
  echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections; \
  apt-get install -y --no-install-recommends ttf-mscorefonts-installer; \
  fc-cache -f -v; \
  rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
