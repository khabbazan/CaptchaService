FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Models and tokenizers live in the mounted volume, not in the image.
    HF_HOME=/models/huggingface

WORKDIR /srv

# CPU-only torch keeps the image small (about 1 GB instead of several).
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

COPY app ./app

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
