# Captcha solver

A small OCR service for login captchas. You send the **kind** of captcha (`digits` or `math`) and the image; it reads
it with the matching model and returns the reading with **every feature of the text**. It can also fetch a captcha from a
provider for you (`GET /captcha/{platform}`, see below).

| Kind | Looks like | Model |
|---|---|---|
| `math` | `12+3=?`, the answer is the result | `arkhabbazan/ocr-math-captcha` |
| `digits` | plain digits (4 to 7, set per provider) | `arkhabbazan/ocr-digits-captcha` |

## Run

```bash
cp .env.example .env                          # HF_TOKEN: Hugging Face READ token (the models are private); API_TOKEN: optional, enables the token check
cp providers.example.json providers.json      # then edit it: the captcha providers you want to use (see Providers)
docker compose up -d --build
docker compose logs -f        # the first start downloads ~2.5 GB; /health is 200 when ready
```

**The models are downloaded once.** They are stored in the named volume `captcha_solver_models`
(`HF_HOME=/models/huggingface`), so rebuilding the image, `docker compose down` or restarting never downloads
them again. Only `docker compose down -v` / `docker volume rm captcha_solver_models` removes them.
To download without starting the API: `docker compose run --rm download-models`.
The download resumes after a dropped connection and retries automatically.

## API

Interactive docs: `http://localhost:8002/docs`.

**Token.** If the `API_TOKEN` environment variable is set, every endpoint except `/health` needs that token, sent as
`Authorization: Bearer <token>` (or `X-API-Token: <token>`). A missing or wrong token gets **403**.
The examples below show the header; leave it out when no token is set.
If `API_TOKEN` is not set (or empty), no token is checked and every request is accepted.

### `POST /solve` - read a captcha image

Input:

```bash
curl -X POST http://localhost:8002/solve \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"kind": "digits", "length": 4, "image": "<base64 or data:image/png;base64,...>"}'
```

| Field | | |
|---|---|---|
| `kind` | required | `"digits"` (alias `"digit"`) or `"math"` |
| `image` | required | base64, with or without the `data:image/...;base64,` prefix, at most 300 KB |
| `length` | optional | digits only: the exact digit count, if you know it (the provider's digit count). Without it any 4 to 7 digits is accepted |

Output:

```json
{
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

`text` is what the model read, `answer` is what to type, `valid_format: false` means the reading has the wrong length or is not
an expression (ask for a new captcha), and `confidence` is the lowest probability the model gave to any character.

Errors: `403` bad or missing token, `400` the image cannot be read, `422` bad `kind` or `length`, `503` models still loading.

### `GET /captcha/{platform}` - fetch a captcha from a provider and read it

```bash
curl -H "Authorization: Bearer $API_TOKEN" 'http://localhost:8002/captcha/SITE_B?image=false'
```

```json
{
  "platform": "SITE_B",
  "kind": "digits",
  "attempts": 1,
  "meets_min_confidence": true,
  "captcha": {
    "token": "…",          // captcha key (styles `token_image`, `token_cookies`)
    "hash": null,          // hashedCaptcha + salt (style `hash_salt`)
    "salt": null,
    "cookies": {"…": "…"}, // cookies the provider set; send them back with the login (style `token_cookies`)
    "fetch_ms": 412,
    "image": "data:image/png;base64,…"   // omit with ?image=false
  },
  "text": {
    "kind": "digits",
    "model": "arkhabbazan/ocr-digits-captcha",
    "text": "2199",          // what the model read
    "answer": "2199",        // what to type: the digits, or the result of a math expression
    "valid_format": true,    // false = wrong length / not an expression: fetch another captcha
    "confidence": 0.9903,    // lowest probability the model gave to any character
    "length": 4,
    "expected_length": 4,
    "tokens": [{"token": "2199", "probability": 0.9903}],
    "views_tried": 1,
    "elapsed_ms": 980
  }
}
```

Math readings also carry `"expression": {"left": 65, "operator": "+", "right": 3}` and `"answer": "68"`.

`{platform}` is a provider `name` from `providers.json`, case-insensitive. Query parameters: `image` (default `true`), `min_confidence` (0-1) and `max_attempts` (1-10). With
`min_confidence=0.9&max_attempts=4` it fetches new captchas until a reading is that confident and returns the best
one **with the token and image of that very captcha**.

Errors: `403` bad or missing token, `404` unknown platform (or no providers configured), `502` the provider is unreachable or returned nothing usable, `503` models still loading.

### Other endpoints

* `GET /platforms` lists the providers with their kind and digit length.
* `GET /health` needs no token: `200` when both models are loaded, otherwise `503` with the reason.

## Providers

Providers are not in the code. Without a `providers.json` the service still starts, but `GET /captcha/{platform}` has no platforms (`POST /solve` is unaffected). Copy `providers.example.json` to `providers.json` (git-ignored, mounted into the
container) and list yours: `name`, `kind` (`math`/`digits`), `url`, `headers`, optional `length`, and `style`
(the shape of the provider's JSON):

| `style` | Provider response read as |
|---|---|
| `token_image` | `response.data.token` + `response.data.image` |
| `token_cookies` | `Data.CaptchaKey` + `Data.Captcha` (image) + the cookies it sets |
| `hash_salt` | `hashedCaptcha` + `salt` + `captchaByteData` (image) |

Path override: `PROVIDERS_FILE`.

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `HF_TOKEN` | empty | Hugging Face read token (needed while the model repos are private) |
| `API_TOKEN` | empty | if set, the token callers must send (wrong or missing = 403); if empty, no check |
| `PORT` | `8002` | published port |
| `MATH_MODEL_ID` / `DIGITS_MODEL_ID` | the two repos above | point at newer model versions |
| `PROVIDER_TIMEOUT` | `10` | seconds to wait for a provider |
| `PROVIDERS_FILE` | `providers.json` | path of the providers list |
| `DIGIT_CONFIDENT` | `0.6` | below this the digits model also tries contrast-normalised views |
| `MAX_IMAGE_BYTES` | `307200` | largest accepted image (300 KB) |
| `WARMUP` | `true` | run a blank image through both models at startup |
| `DOWNLOAD_ATTEMPTS` | `15` | tries per model download (resumes each time) |
| `HF_HUB_OFFLINE` | `0` | `1` = never contact Hugging Face once the models are in the volume |

To pick up a **new version of a model** (same repo id), clear the cached files once:
`docker compose run --rm --entrypoint sh download-models -c 'rm -rf /models/huggingface/hub/models--arkhabbazan--ocr-digits-captcha'` and restart.

## Develop / test

```bash
pip install -r requirements.txt pytest
pytest                          # fast tests, no models, network or providers.json needed
uvicorn app.main:app --port 8002   # reads providers.json from the current directory
```

## Notes

* The captcha token is only valid for the captcha it came with, and the provider expires it quickly. Use the response
  right away.
* Accuracy on **real** captchas is not guaranteed: check `valid_format` and `confidence`, and use
  `min_confidence`/`max_attempts` to retry.
* Set a long random `API_TOKEN` (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) and do not expose the port publicly without it (with no token anyone can use the service).
