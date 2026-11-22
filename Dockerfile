FROM python:3.12-slim
# ffmpeg + fonts (Noto Sans Bengali is needed for Bangla subtitles)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fontconfig \
    && fc-cache -f && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "app.py"]
