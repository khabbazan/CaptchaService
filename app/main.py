import base64
import binascii
import logging
import secrets
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import config
from app.ocr import ImageError, OcrEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="Captcha solver",
    description="Reads a captcha image with the OCR models and returns the reading together with the caller's title.",
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


KINDS = {"math": "math", "digits": "digits", "digit": "digits"}


class SolveRequest(BaseModel):
    image: str                       # base64 or data URI
    kind: str                        # "digits" (alias "digit") or "math"
    length: int | None = None        # digits only: exact digit count, when you know it
    title: str | None = None         # free label chosen by the caller, returned unchanged in the response


@app.post("/solve", dependencies=[Depends(require_token), Depends(require_ready)])
async def solve(body: SolveRequest):
    """
    Read a captcha image. Give the **kind** (`digits` or `math`) and the image; for digit captchas you may
    also give the exact `length`. An optional `title` (any string) is returned unchanged with the reading.
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
    result = await run_in_threadpool(engine.solve, image, kind, body.length if kind == "digits" else None)
    return {"title": body.title, **result}
