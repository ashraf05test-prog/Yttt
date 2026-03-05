FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

RUN pip install yt-dlp flask flask-session requests gunicorn

WORKDIR /app
COPY . .

RUN mkdir -p temp outputs

EXPOSE 8080
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 300
