# Relay hub. On Railway: set the service's Root Directory to `relay`, attach a Volume
# at /data, and set RELAY_ADMIN_TOKEN as a variable. The worker node does NOT run here -
# it runs on a machine with yt-dlp and ffmpeg (see README).
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.164.0 uvicorn==0.54.0
COPY __init__.py hub.py cli.py ui.html ui.js /app/relay/
COPY logos/*.svg /app/relay/logos/
ENV RELAY_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8787
# Railway injects $PORT; default to 8787 elsewhere
CMD ["sh", "-c", "exec uvicorn relay.hub:app --host 0.0.0.0 --port ${PORT:-8787} --proxy-headers --forwarded-allow-ips='*'"]
