"""
OCR engine: one TrOCR model for math captchas and one for digit captchas.

Both models are loaded once at startup from the Hugging Face cache (HF_HOME, the compose volume), so
they are only downloaded the very first time.
"""
import io
import logging
import math
import os
import re
import threading
import time

import numpy as np
import torch
from PIL import Image, ImageOps
from huggingface_hub import HfApi, constants as hub_constants, snapshot_download
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

from app import config

logger = logging.getLogger("captcha.ocr")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
FULL_MATH = re.compile(r"^(\d+)([+-])(\d+)=\?$")


class ImageError(ValueError):
    """The bytes are not a usable image."""


def _folder_size(path):
    """Total bytes of the files under `path` (also the .incomplete ones of a running download)."""
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:   # a partial file was just renamed
                pass
    return total


class _ProgressLogger(threading.Thread):
    """While a model downloads, log its progress every 10 % by watching the blob folder of the Hub cache grow."""

    def __init__(self, model_id, total, interval=2.0):
        super().__init__(daemon=True, name="download-progress")
        self.model_id, self.total, self.interval = model_id, total, interval
        self.blobs = os.path.join(hub_constants.HF_HUB_CACHE, "models--" + model_id.replace("/", "--"), "blobs")
        self.start_bytes = self._size()
        self.logged = self._step(self.start_bytes)     # a resumed download only reports the steps still to come
        self.downloading = self.start_bytes < total
        self._stopped = threading.Event()

    def _size(self):
        return _folder_size(self.blobs) if os.path.isdir(self.blobs) else 0

    def _step(self, size):
        return min(100, size * 100 // self.total) // 10 * 10

    def report(self, final=False):
        size = self._size()
        step = 100 if final else self._step(size)
        if step > self.logged:
            self.logged = step
            logger.info("Downloading %s: %d%% (%.0f of %.0f MB)", self.model_id, step, min(size, self.total) / 1e6, self.total / 1e6)

    def run(self):
        while not self._stopped.wait(self.interval):
            self.report()

    def finish(self):
        self._stopped.set()
        if self.downloading:
            self.report(final=True)


def _download_plan(model_id):
    """(file names, total bytes) of the files the model needs, or (None, 0) if the Hub cannot be asked."""
    try:
        siblings = HfApi().model_info(model_id, files_metadata=True, token=config.HF_TOKEN).siblings
    except Exception:
        return None, 0
    sizes = {s.rfilename: s.size or 0 for s in siblings}
    has_safetensors = any(name.endswith(".safetensors") for name in sizes)
    unneeded = (".h5", ".msgpack", ".ot", ".onnx") + ((".bin", ".pt", ".pth") if has_safetensors else ())
    sizes = {name: size for name, size in sizes.items() if not name.endswith(unneeded) and not name.startswith("checkpoint-")}
    return list(sizes), sum(sizes.values())


class OcrEngine:
    def __init__(self):
        self.model_ids = {"math": config.MATH_MODEL_ID, "digits": config.DIGITS_MODEL_ID}
        self.experts = {}
        self.ready = False
        self.error = None
        self.loaded_at = None
        # The torch models are not worth running concurrently.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ loading
    @staticmethod
    def _retry(loader):
        """Downloads resume from the partial file, so a dropped connection only costs another try."""
        for attempt in range(1, config.DOWNLOAD_ATTEMPTS + 1):
            try:
                return loader()
            except Exception as e:
                if attempt == config.DOWNLOAD_ATTEMPTS:
                    raise
                logger.warning("Model download failed (%s), retry %d/%d", e.__class__.__name__, attempt, config.DOWNLOAD_ATTEMPTS - 1)
                time.sleep(5)

    @staticmethod
    def _download(model_id):
        """Make sure the model is on disk, log the progress every 10 % and return the folder it is saved in."""
        files, total = _download_plan(model_id)
        progress = None
        if total:
            logger.info("Checking %s on the Hub: %.0f MB in %d files", model_id, total / 1e6, len(files))
            progress = _ProgressLogger(model_id, total)
            progress.start()
        try:
            path = snapshot_download(model_id, token=config.HF_TOKEN, allow_patterns=files)
        finally:
            if progress:
                progress.finish()
        outcome = "ready" if progress is None else ("downloaded" if progress.downloading else "found on disk, nothing to download")
        logger.info("Model %s %s, saved at %s (%.0f MB)", model_id, outcome, path, _folder_size(path) / 1e6)
        return path

    def load(self):
        try:
            logger.info("Models are stored in %s", hub_constants.HF_HUB_CACHE)
            for kind, model_id in self.model_ids.items():
                logger.info("Loading %s model %s", kind, model_id)
                path = self._retry(lambda: self._download(model_id))
                processor = TrOCRProcessor.from_pretrained(path)
                model = VisionEncoderDecoderModel.from_pretrained(path)
                self.experts[kind] = (processor, model.to(DEVICE).eval())
            if config.WARMUP:
                blank = Image.new("RGB", (160, 60), "white")
                for kind in self.experts:
                    self._read(blank, kind, None)
            self.ready = True
            self.loaded_at = time.time()
            logger.info("OCR models ready on %s", DEVICE)
        except Exception as e:
            logger.exception("Failed to load the OCR models")
            self.error = f"{e.__class__.__name__}: {e}"

    # ------------------------------------------------------------------ images
    @staticmethod
    def open_image(raw):
        if len(raw) > config.MAX_IMAGE_BYTES:
            raise ImageError("Image too large.")
        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception as e:
            raise ImageError("Not a valid image.") from e
        # Transparent PNGs must sit on white, as in training; a plain convert makes them black.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            white = Image.new("RGBA", image.size, (255, 255, 255, 255))
            white.alpha_composite(image)
            return white.convert("RGB")
        return image.convert("RGB")

    @staticmethod
    def _views(image):
        original = image.convert("RGB")
        gray = ImageOps.autocontrast(original.convert("L"))
        views = [original, gray.convert("RGB")]
        if float(np.asarray(gray, dtype=np.float32).mean()) < 128.0:
            views.append(ImageOps.invert(gray).convert("RGB"))
        return views

    # ------------------------------------------------------------------ decoding
    @staticmethod
    def _normalize(text):
        return str(text).replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "").strip()

    def _generate(self, kind, image, max_length):
        processor, model = self.experts[kind]
        values = processor(images=image, return_tensors="pt").pixel_values.to(DEVICE)
        with torch.inference_mode():
            sequences = model.generate(values, num_beams=4, max_length=max_length)
            text = self._normalize(processor.batch_decode(sequences, skip_special_tokens=True)[0])
            confidence, tokens = self._confidence(processor, model, values, sequences)
        return text, confidence, tokens

    @staticmethod
    def _confidence(processor, model, pixel_values, sequences):
        """
        Teacher-forced pass over the decoded sequence: the probability the model gave to each real
        character. The lowest one is the confidence of the whole reading, so a single unsure digit
        pulls it down.
        """
        logits = model(pixel_values=pixel_values, decoder_input_ids=sequences).logits[0, :-1]
        probabilities = torch.softmax(logits.float(), dim=-1)
        targets = sequences[0, 1:]
        chosen = probabilities.gather(1, targets.unsqueeze(1)).squeeze(1)
        special = set(processor.tokenizer.all_special_ids)
        tokens = [
            {"token": processor.tokenizer.decode([int(t)], skip_special_tokens=True).strip(), "probability": round(float(p), 4)}
            for t, p in zip(targets.tolist(), chosen.tolist()) if t not in special
        ]
        return (min(t["probability"] for t in tokens) if tokens else 0.0), tokens

    def _read(self, image, kind, length):
        """Returns (text, confidence, tokens, views_tried)."""
        if kind == "math":
            text, confidence, tokens = self._generate("math", image, 32)
            if re.fullmatch(r"\d+[+-]\d+=", text):
                text += "?"
            return text, confidence, tokens, 1

        # The fine-tuned model must be free to emit its start token, so decoding is not constrained to
        # digit tokens; the result is validated against the expected digit count instead.
        min_digits, max_digits = (length, length) if length else (4, 7)
        pattern = re.compile(rf"^\d{{{min_digits},{max_digits}}}$")
        views = self._views(image)
        candidates = [self._generate("digits", views[0], 16)]
        if not (pattern.fullmatch(candidates[0][0]) and candidates[0][1] >= config.DIGIT_CONFIDENT):
            # Unsure or malformed: also try the contrast-normalised views and keep the best valid reading.
            candidates += [self._generate("digits", view, 16) for view in views[1:]]
        valid = [c for c in candidates if pattern.fullmatch(c[0])]
        text, confidence, tokens = max(valid or candidates, key=lambda c: c[1])
        return text, confidence, tokens, len(candidates)

    # ------------------------------------------------------------------ public
    def solve(self, image, kind, length=None):
        """Read one captcha image (PIL, already on a white background). Blocking."""
        if not self.ready:
            raise RuntimeError("Models are not loaded.")
        started = time.perf_counter()
        with self._lock:
            text, confidence, tokens, views = self._read(image, kind, length)
        elapsed_ms = round((time.perf_counter() - started) * 1000)

        if kind == "math":
            match = FULL_MATH.match(text)
            valid = bool(match)
        else:
            valid = bool(re.fullmatch(rf"\d{{{length}}}" if length else r"\d{4,7}", text))
            match = None

        result = {
            "kind": kind,
            "model": self.model_ids[kind],
            "text": text,
            "answer": "",
            "valid_format": valid,
            "confidence": round(confidence, 4),
            "length": len(text),
            "expected_length": length,
            "tokens": tokens,
            "views_tried": views,
            "elapsed_ms": elapsed_ms,
        }
        if valid and kind == "math":
            left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
            result["answer"] = str(left + right if operator == "+" else left - right)
            result["expression"] = {"left": left, "operator": operator, "right": right}
        elif valid:
            result["answer"] = text
        return result
