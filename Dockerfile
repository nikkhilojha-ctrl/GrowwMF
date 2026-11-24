FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf \
    DATABASE_PATH=/data/assistant.db

WORKDIR /app

# CPU-only torch keeps the image near 1 GB instead of ~4 GB with CUDA wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image so cold starts do not download it.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY app ./app
COPY corpus ./corpus
COPY static ./static
COPY scripts/start.sh ./scripts/start.sh
RUN sed -i 's/\r$//' scripts/start.sh && chmod +x scripts/start.sh

EXPOSE 8000
CMD ["./scripts/start.sh"]
