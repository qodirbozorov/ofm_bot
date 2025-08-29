FROM python:3.11-slim

# System deps: LibreOffice + fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-common libreoffice-writer fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
