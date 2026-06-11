FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git ffmpeg libsm6 libxext6 gcc g++ \
    portaudio19-dev libsndfile1 libassimp-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app

ENV PATH="/home/user/.local/bin:$PATH"
ENV DATA_DIR="/data"
ENV PORT="7860"

COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user . .

EXPOSE 7860

# WICHTIG: Direkt uvicorn, KEIN gunicorn!
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 7860 --workers 1 --log-level info"]