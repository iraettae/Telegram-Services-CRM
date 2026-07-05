FROM python:3.12-slim

WORKDIR /app

# System deps (tgcrypto needs build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Create dirs for persistent data
RUN mkdir -p /app/sessions

EXPOSE 8080

CMD ["python", "main.py"]
