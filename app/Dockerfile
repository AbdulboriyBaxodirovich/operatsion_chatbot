FROM python:3.11-slim

WORKDIR /app

# 1. LibreOffice o'rnatish
RUN apt-get update && apt-get install -y \
    libreoffice \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# 2. Requirements o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Backend kodini ko'chirish
COPY backend/ ./backend/

# 4. Frontend statik fayllarini ko'chirish
COPY frontend/ ./frontend/

EXPOSE 8080

# FastAPI serverni ishga tushirish
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]