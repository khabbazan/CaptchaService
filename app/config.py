import os


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Models. Both repositories are downloaded once into HF_HOME (the compose volume).
MATH_MODEL_ID = os.environ.get("MATH_MODEL_ID", "arkhabbazan/ocr-math-captcha")
DIGITS_MODEL_ID = os.environ.get("DIGITS_MODEL_ID", "arkhabbazan/ocr-digits-captcha")

# Read token, needed while a model repository is private. Empty = anonymous download.
HF_TOKEN = os.environ.get("HF_TOKEN") or False

# Access token. Every endpoint except /health needs it (header `Authorization: Bearer <token>` or
# `X-API-Token: <token>`); a missing or wrong token gets 403. Unset or empty = no token check at all.
API_TOKEN = os.environ.get("API_TOKEN", "")

# Provider requests.
PROVIDER_TIMEOUT = float(os.environ.get("PROVIDER_TIMEOUT", "10"))

# JSON file listing the captcha providers (see providers.example.json).
PROVIDERS_FILE = os.environ.get("PROVIDERS_FILE", "providers.json")

# OCR.
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(300 * 1024)))
# A digits reading at or above this confidence is accepted without trying the contrast-normalised views.
DIGIT_CONFIDENT = float(os.environ.get("DIGIT_CONFIDENT", "0.6"))
DOWNLOAD_ATTEMPTS = int(os.environ.get("DOWNLOAD_ATTEMPTS", "15"))
WARMUP = _bool("WARMUP", True)
