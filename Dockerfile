FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    python3-dev \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python3 -c "from nudenet import NudeDetector; NudeDetector(); from rapidocr_onnxruntime import RapidOCR; RapidOCR(); print('NSFW ML models verified!')"

COPY . .

CMD ["python3", "bot.py"]

