"""API tests with a fake OCR engine: no models, no network."""
import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main


def png_b64():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class FakeEngine:
    ready = True
    error = None
    model_ids = {"math": "m", "digits": "d"}

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = []

    def solve(self, image, kind, length=None):
        self.calls.append((kind, length))
        text, confidence = self.readings.pop(0) if self.readings else ("1234", 0.99)
        return {"kind": kind, "model": "x", "text": text, "answer": text, "valid_format": True,
                "confidence": confidence, "length": len(text), "expected_length": length,
                "tokens": [], "views_tried": 1, "elapsed_ms": 1}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "engine", FakeEngine([]))
    monkeypatch.setattr(main.config, "API_TOKEN", "secret")
    # TestClient without the startup hook (it would start the real model loader).
    app = main.app
    app.router.on_startup.clear()
    return TestClient(app, headers={"Authorization": "Bearer secret"})


def body(**extra):
    return {"image": png_b64(), "kind": "digits", **extra}


def test_title_is_returned_unchanged(client):
    r = client.post("/solve", json=body(title="login-42"))
    assert r.status_code == 200 and r.json()["title"] == "login-42" and r.json()["answer"] == "1234"


def test_title_is_optional(client):
    assert client.post("/solve", json=body()).json()["title"] is None


def test_wrong_or_missing_token_is_403(client):
    anonymous = TestClient(main.app)
    assert anonymous.post("/solve", json=body()).status_code == 403
    assert anonymous.post("/solve", json=body(), headers={"Authorization": "Bearer nope"}).status_code == 403
    assert anonymous.post("/solve", json=body(), headers={"X-API-Token": "secret"}).status_code == 200
    assert anonymous.post("/solve", json=body(), headers={"Authorization": "Bearer secret"}).status_code == 200
    assert anonymous.get("/health").status_code == 200   # health stays open


def test_no_token_configured_skips_the_check(client, monkeypatch):
    monkeypatch.setattr(main.config, "API_TOKEN", "")
    assert TestClient(main.app).post("/solve", json=body()).status_code == 200   # no header at all


def test_503_while_models_load(client, monkeypatch):
    loading = FakeEngine([])
    loading.ready = False
    monkeypatch.setattr(main, "engine", loading)
    assert client.post("/solve", json=body()).status_code == 503
    assert client.get("/health").status_code == 503


def test_solve_takes_the_kind(client):
    uri = "data:image/jpeg;base64," + png_b64()
    r = client.post("/solve", json={"image": uri, "kind": "math"})
    assert r.status_code == 200 and main.engine.calls[-1] == ("math", None)


def test_bad_kind_length_and_image(client):
    assert client.post("/solve", json=body(kind="nope")).status_code == 422
    assert client.post("/solve", json=body(length=99)).status_code == 422
    assert client.post("/solve", json={"image": "AAAA", "kind": "digits"}).status_code == 400
