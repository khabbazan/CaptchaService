import base64
import io
import binascii
import logging
import secrets
import threading

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from PIL import Image
from pydantic import BaseModel

from app import config
from app.ocr import ImageError, OcrEngine
from app.providers import PROVIDERS, ProviderError, fetch_captcha

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="Captcha solver",
    description="Fetches a captcha from a provider, reads it with the OCR models and returns the captcha token and the reading.",
)
engine = OcrEngine()


@app.on_event("startup")
def startup():
    threading.Thread(target=engine.load, daemon=True, name="model-loader").start()


def require_token(request: Request, x_api_token: str = Header(default="")):
    """If API_TOKEN is set, 403 unless the request carries it. If it is not set, no check is done."""
    if not config.API_TOKEN:
        return
    bearer = request.headers.get("authorization", "")
    supplied = x_api_token or (bearer[7:] if bearer.lower().startswith("bearer ") else "")
    if not secrets.compare_digest(supplied.encode(), config.API_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Invalid or missing token.")


def require_ready():
    if not engine.ready:
        raise HTTPException(status_code=503, detail=engine.error or "Models are loading.")


def provider_or_404(platform):
    provider = PROVIDERS.get(platform.upper())
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown platform. Choose one of: {', '.join(PROVIDERS)}.")
    return provider


def data_uri(raw):
    image_format = (Image.open(io.BytesIO(raw)).format or "PNG").lower()
    return f"data:image/{'jpeg' if image_format == 'jpg' else image_format};base64,{base64.b64encode(raw).decode()}"


# ---------------------------------------------------------------------------- info
@app.get("/health")
def health():
    body = {
        "status": "ready" if engine.ready else ("failed" if engine.error else "loading"),
        "models": engine.model_ids,
        "error": engine.error,
    }
    if not engine.ready:
        raise HTTPException(status_code=503, detail=body)
    return body


@app.get("/platforms", dependencies=[Depends(require_token)])
def platforms():
    return [{"platform": p.name, "kind": p.kind, "length": p.length} for p in PROVIDERS.values()]


# ---------------------------------------------------------------------------- main endpoint
@app.get("/captcha/{platform}", dependencies=[Depends(require_token), Depends(require_ready)])
async def captcha(
    platform: str,
    image: bool = Query(True, description="Include the captcha image (data URI) in the response."),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0, description="Fetch a new captcha until the reading is at least this confident."),
    max_attempts: int = Query(1, ge=1, le=10, description="Maximum captchas to fetch while chasing min_confidence."),
):
    """
    Fetch a captcha from the provider and read it. Returns the captcha token (token, or hash and salt, plus
    any cookies the provider set) together with every text feature of the reading.

    With `max_attempts > 1` the best reading across the attempts is returned, together with the token
    and image of that very captcha.
    """
    provider = provider_or_404(platform)
    best = None
    attempts = 0
    async with httpx.AsyncClient() as client:
        for _ in range(max_attempts):
            attempts += 1
            try:
                fetched = await fetch_captcha(provider.name, client)
            except ProviderError as e:
                if best is None and attempts == max_attempts:
                    raise HTTPException(status_code=502, detail=str(e))
                continue
            try:
                reading = await run_in_threadpool(
                    engine.solve, OcrEngine.open_image(fetched.image_bytes), provider.kind, provider.length)
            except ImageError as e:
                if best is None and attempts == max_attempts:
                    raise HTTPException(status_code=502, detail=f"{provider.name} returned {e}")
                continue
            rank = (reading["valid_format"], reading["confidence"])
            if best is None or rank > best[0]:
                best = (rank, fetched, reading)
            if reading["valid_format"] and reading["confidence"] >= min_confidence:
                break
    if best is None:
        raise HTTPException(status_code=502, detail=f"{provider.name} did not return a usable captcha.")

    _, fetched, reading = best
    return {
        "platform": provider.name,
        "kind": provider.kind,
        "attempts": attempts,
        "meets_min_confidence": reading["valid_format"] and reading["confidence"] >= min_confidence,
        "captcha": {
            "token": fetched.token,
            "hash": fetched.hash,
            "salt": fetched.salt,
            "cookies": fetched.cookies,
            "fetch_ms": fetched.fetch_ms,
            **({"image": data_uri(fetched.image_bytes)} if image else {}),
        },
        "text": reading,
    }


# ---------------------------------------------------------------------------- read a captcha image
KINDS = {"math": "math", "digits": "digits", "digit": "digits"}


class SolveRequest(BaseModel):
    image: str                       # base64 or data URI
    kind: str                        # "digits" (alias "digit") or "math"
    length: int | None = None        # digits only: exact digit count, when you know it


@app.post("/solve", dependencies=[Depends(require_token), Depends(require_ready)])
async def solve(body: SolveRequest):
    """
    Read a captcha image. Give the **kind** (`digits` or `math`) and the image; for digit captchas you may
    also give the exact `length`. Returns the reading and every text feature of it.
    """
    kind = KINDS.get(body.kind.strip().lower())
    if kind is None:
        raise HTTPException(status_code=422, detail="kind must be 'digits' or 'math'.")
    if body.length is not None and not 1 <= body.length <= 12:
        raise HTTPException(status_code=422, detail="length must be between 1 and 12.")
    try:
        raw = base64.b64decode(body.image.split(",", 1)[-1], validate=False)
        image = OcrEngine.open_image(raw)
    except (binascii.Error, ImageError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "Invalid image.")
    return await run_in_threadpool(engine.solve, image, kind, body.length if kind == "digits" else None)
