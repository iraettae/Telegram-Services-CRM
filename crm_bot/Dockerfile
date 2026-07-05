FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Create dirs for persistent data
RUN mkdir -p /app/logs /app/frontend/avatars /app/frontend/media

EXPOSE 8000

CMD ["python", "main.py"]
