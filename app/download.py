"""Download both models into the cache without starting the API:  python -m app.download"""
import logging

from app.ocr import OcrEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    engine = OcrEngine()
    engine.load()
    raise SystemExit(0 if engine.ready else f"Download failed: {engine.error}")
