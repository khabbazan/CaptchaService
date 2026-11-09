"""API tests with a fake OCR engine and a fake provider: no models, no network."""
import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.providers import Captcha, Provider, ProviderError


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
    monkeypatch.setattr(main, "PROVIDERS", {
        "SITE_A": Provider("SITE_A", "math", "https://a.example", {}),
        "SITE_B": Provider("SITE_B", "digits", "https://b.example", {}, length=4, style="token_cookies"),
    })
    monkeypatch.setattr(main, "engine", FakeEngine([]))
    monkeypatch.setattr(main.config, "API_TOKEN", "secret")

    async def fetch(platform, client=None):
        return Captcha(platform, png_b64(), token="T", cookies={"a": "b"})
    monkeypatch.setattr(main, "fetch_captcha", fetch)
    # TestClient without the startup hook (it would start the real model loader).
    app = main.app
    app.router.on_startup.clear()
    return TestClient(app, headers={"Authorization": "Bearer secret"})


def test_captcha_returns_token_and_reading(client):
    body = client.get("/captcha/site_b").json()
    assert body["platform"] == "SITE_B" and body["kind"] == "digits"
    assert body["captcha"]["token"] == "T" and body["captcha"]["cookies"] == {"a": "b"}
    assert body["captcha"]["image"].startswith("data:image/png;base64,")
    assert body["text"]["expected_length"] == 4


def test_image_can_be_left_out(client):
    assert "image" not in client.get("/captcha/SITE_A?image=false").json()["captcha"]


def test_unknown_platform_is_404(client):
    assert client.get("/captcha/NOPE").status_code == 404


def test_provider_failure_is_502(client, monkeypatch):
    async def broken(platform, client=None):
        raise ProviderError("down")
    monkeypatch.setattr(main, "fetch_captcha", broken)
    assert client.get("/captcha/SITE_B").status_code == 502


def test_min_confidence_keeps_the_best_attempt(client, monkeypatch):
    monkeypatch.setattr(main, "engine", FakeEngine([("1111", 0.3), ("2222", 0.95)]))
    body = client.get("/captcha/SITE_B?min_confidence=0.9&max_attempts=3&image=false").json()
    assert body["attempts"] == 2 and body["text"]["text"] == "2222" and body["meets_min_confidence"]


def test_wrong_or_missing_token_is_403(client, monkeypatch):
    anonymous = TestClient(main.app)
    assert anonymous.get("/platforms").status_code == 403
    assert anonymous.get("/platforms", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert anonymous.post("/solve", json={"image": png_b64(), "kind": "digits"}).status_code == 403
    assert anonymous.get("/platforms", headers={"X-API-Token": "secret"}).status_code == 200
    assert anonymous.get("/platforms", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert anonymous.get("/health").status_code == 200   # health stays open


def test_no_token_configured_skips_the_check(client, monkeypatch):
    monkeypatch.setattr(main.config, "API_TOKEN", "")
    assert TestClient(main.app).get("/platforms").status_code == 200   # no header at all
    assert client.post("/solve", json={"image": png_b64(), "kind": "digits"}).status_code == 200


def test_503_while_models_load(client, monkeypatch):
    loading = FakeEngine([])
    loading.ready = False
    monkeypatch.setattr(main, "engine", loading)
    assert client.get("/captcha/SITE_B").status_code == 503
    assert client.get("/health").status_code == 503


def test_solve_takes_the_kind_not_a_platform(client):
    uri = "data:image/jpeg;base64," + png_b64()
    r = client.post("/solve", json={"image": uri, "kind": "math"})
    assert r.status_code == 200 and main.engine.calls[-1] == ("math", None)
    r = client.post("/solve", json={"image": uri, "kind": "Digit", "length": 5})   # alias, any case
    assert r.status_code == 200 and main.engine.calls[-1] == ("digits", 5)
    assert client.post("/solve", json={"image": uri, "kind": "digits"}).status_code == 200
    assert main.engine.calls[-1] == ("digits", None)


def test_solve_rejects_bad_input(client):
    assert client.post("/solve", json={"image": png_b64(), "kind": "letters"}).status_code == 422
    assert client.post("/solve", json={"image": png_b64(), "kind": "digits", "length": 99}).status_code == 422
    assert client.post("/solve", json={"image": png_b64()}).status_code == 422          # kind is required
    assert client.post("/solve", json={"image": "AAAA", "kind": "digits"}).status_code == 400
