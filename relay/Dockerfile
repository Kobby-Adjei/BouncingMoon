# Relay hub. On Railway: set the service's Root Directory to `relay`, attach a Volume at /data, and
# set RELAY_ADMIN_TOKEN as a variable (used once, to claim the hub). Media is watched here: ffmpeg makes
# contact sheets and playable copies, and the speech model below writes the transcripts.
FROM python:3.12-slim
WORKDIR /app

# ffmpeg and the fonts its contact-sheet labels are drawn with. The build stops here if either is missing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-montserrat fonts-liberation \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -hide_banner -filters | grep -q " ass " \
 && test -f /usr/share/fonts/opentype/montserrat/Montserrat-ExtraBold.otf \
 && test -f /usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf

RUN pip install --no-cache-dir fastapi==0.164.0 uvicorn==0.54.0 cryptography==50.0.1 faster-whisper==1.2.1

# The English speech model, baked into the image outside /data (the volume would hide it) so the first
# transcript isn't slow and nothing is downloaded at run time. The build stops if it doesn't load.
ARG WHISPER_MODEL=small.en
ENV RELAY_WHISPER_MODEL=${WHISPER_MODEL} RELAY_WHISPER_DIR=/opt/whisper HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "import os; from faster_whisper import download_model; m = os.environ['RELAY_WHISPER_MODEL']; download_model(m, output_dir='/opt/whisper/' + m)" \
 && rm -rf /opt/whisper/*/.cache /root/.cache \
 && python -c "import os; from faster_whisper import WhisperModel; WhisperModel('/opt/whisper/' + os.environ['RELAY_WHISPER_MODEL'], device='cpu', compute_type='int8'); print('speech model loads')"

COPY __init__.py hub.py connector.py wake.py people.py captions.py media.py oauth.py webauthn.py vault.py providers.py backup.py offsite.py push.py cli.py cli.sig.json /app/relay/
COPY logos/*.svg /app/relay/logos/
COPY web/ /app/relay/web/
ENV RELAY_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8787
# Railway injects $PORT; default to 8787 elsewhere
CMD ["sh", "-c", "exec uvicorn relay.hub:app --host 0.0.0.0 --port ${PORT:-8787} --proxy-headers --forwarded-allow-ips='*'"]
