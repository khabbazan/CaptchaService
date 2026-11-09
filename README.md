# Captcha solver

A small OCR service for captchas. You send an image, the **kind** of captcha (`digits` or `math`) and an optional
**title** of your choice; it reads the image with the matching model and returns the reading with **every feature of the
text**, together with the same title.

| Kind | Looks like | Model |
|---|---|---|
| `math` | `12+3=?`, the answer is the result | `arkhabbazan/ocr-math-captcha` |
| `digits` | plain digits (4 to 7) | `arkhabbazan/ocr-digits-captcha` |

## Run

```bash
cp .env.example .env          # set MODELS_DIR (host folder for the models), HF_TOKEN (Hugging Face READ token, the models are private), optionally API_TOKEN
set -a; source .env; set +a   # load .env into the shell so $MODELS_DIR and $PORT are available below
mkdir -p "$MODELS_DIR"

docker build -t captcha-solver .
docker run -d --name captcha-solver --restart unless-stopped \
  -p "${PORT:-8002}:8002" --env-file .env -v "$MODELS_DIR:/models" captcha-solver
docker logs -f captcha-solver  # the first start downloads ~2.5 GB; /health is 200 when ready
```

**The models are downloaded once.** They are stored in `MODELS_DIR` on the host (mounted at `/models`,
`HF_HOME=/models/huggingface`), so rebuilding the image, removing the container or restarting never downloads
them again. Delete that folder to remove them.
To download without starting the API:

```bash
docker run --rm --env-file .env -e WARMUP=false -v "$MODELS_DIR:/models" captcha-solver python -m app.download
```

The download resumes after a dropped connection and retries automatically.
Stop and remove: `docker rm -f captcha-solver`.

## API

Base URL: `http://localhost:8002`.

| Method | Path | Token | What it does |
|---|---|---|---|
| `POST` | `/solve` | yes (if `API_TOKEN` is set) | read a captcha image, return the reading and your `title` |
| `GET` | `/health` | no | `200` when both models are loaded, otherwise `503` with the reason |
| `GET` | `/docs` | no | interactive Swagger UI |
| `GET` | `/redoc` | no | ReDoc documentation |
| `GET` | `/openapi.json` | no | OpenAPI schema |

**Token.** If the `API_TOKEN` environment variable is set, `/solve` needs that token, sent as
`Authorization: Bearer <token>` (or `X-API-Token: <token>`). A missing or wrong token gets **403**.
If `API_TOKEN` is not set (or empty), no token is checked and every request is accepted.
The examples below show the header; leave it out when no token is set.

### `POST /solve` - read a captcha image

Input:

```bash
curl -X POST http://localhost:8002/solve \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "login-42", "kind": "digits", "length": 4, "image": "<base64 or data:image/png;base64,...>"}'
```

| Field | | |
|---|---|---|
| `kind` | required | `"digits"` (alias `"digit"`) or `"math"` |
| `image` | required | base64, with or without the `data:image/...;base64,` prefix, at most 300 KB |
| `title` | optional | any string you choose; it is returned unchanged, so you can match the answer to your request |
| `length` | optional | digits only: the exact digit count, if you know it. Without it any 4 to 7 digits is accepted |

Output:

```json
{
  "title": "login-42",
  "kind": "digits",
  "model": "arkhabbazan/ocr-digits-captcha",
  "text": "8720",
  "answer": "8720",
  "valid_format": true,
  "confidence": 0.9999,
  "length": 4,
  "expected_length": 4,
  "tokens": [{"token": "87", "probability": 0.9999}, {"token": "20", "probability": 1.0}],
  "views_tried": 1,
  "elapsed_ms": 854
}
```

Math readings also carry `"expression": {"left": 65, "operator": "+", "right": 3}` and `"answer": "68"` (the result).

`title` is `null` when you did not send one. `text` is what the model read, `answer` is what to type,
`valid_format: false` means the reading has the wrong length or is not an expression (send a new captcha), and
`confidence` is the lowest probability the model gave to any character.

Errors: `403` bad or missing token, `400` the image cannot be read, `422` bad `kind` or `length`, `503` models still loading.

### `GET /health`

No token needed.

```json
{"status": "ready", "models": {"math": "arkhabbazan/ocr-math-captcha", "digits": "arkhabbazan/ocr-digits-captcha"}, "error": null}
```

`503` while the models load (`"status": "loading"`) or if loading failed (`"status": "failed"`, with the error).

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `HF_TOKEN` | empty | Hugging Face read token (needed while the model repos are private) |
| `API_TOKEN` | empty | if set, the token callers must send (wrong or missing = 403); if empty, no check |
| `MODELS_DIR` | none, required | host folder where the models are stored (used in the `docker run -v` above, not read by the app) |
| `PORT` | `8002` | published host port (used in the `docker run -p` above, not read by the app) |
| `MATH_MODEL_ID` / `DIGITS_MODEL_ID` | the two repos above | point at newer model versions |
| `DIGIT_CONFIDENT` | `0.6` | below this the digits model also tries contrast-normalised views |
| `MAX_IMAGE_BYTES` | `307200` | largest accepted image (300 KB) |
| `WARMUP` | `true` | run a blank image through both models at startup |
| `DOWNLOAD_ATTEMPTS` | `15` | tries per model download (resumes each time) |
| `HF_HUB_OFFLINE` | `0` | `1` = never contact Hugging Face once the models are in the volume |

To pick up a **new version of a model** (same repo id), delete its cached folder once and restart:
`rm -rf "$MODELS_DIR/huggingface/hub/models--arkhabbazan--ocr-digits-captcha"` (or `...ocr-math-captcha`), then `docker restart captcha-solver`.

## Develop / test

```bash
pip install -r requirements.txt pytest
pytest                          # fast tests, no models or network needed
uvicorn app.main:app --port 8002
```

## Notes

* Accuracy on **real** captchas is not guaranteed: check `valid_format` and `confidence`.
* Set a long random `API_TOKEN` (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) and do not expose the port publicly without it (with no token anyone can use the service).
