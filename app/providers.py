"""
Captcha providers: where each captcha comes from, what kind it is, and how to read the response.

kind   "math"   -> an expression such as 12+3=? (the answer is the result)
       "digits" -> plain digits, `length` of them
"""
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

from app import config

logger = logging.getLogger("captcha.providers")


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    url: str
    headers: dict
    length: int | None = None
    # How the JSON maps to a captcha: "token_image" (response.data.token + image), "token_cookies" (Data.Captcha/CaptchaKey + cookies), "hash_salt" (hash + salt)
    style: str = "token_image"


def load_providers(path):
    """Read the provider list from a JSON file (see providers.example.json). A missing file means no providers."""
    if not os.path.exists(path):
        logger.warning("Providers file %s not found: /captcha/{platform} has no platforms", path)
        return {}
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    return {p["name"].upper(): Provider(**{**p, "name": p["name"].upper()}) for p in items}


PROVIDERS = load_providers(config.PROVIDERS_FILE)


class ProviderError(Exception):
    """The provider did not return a usable captcha."""


@dataclass
class Captcha:
    platform: str
    image_b64: str
    token: str | None = None
    hash: str | None = None
    salt: str | None = None
    cookies: dict = field(default_factory=dict)
    fetch_ms: int = 0

    @property
    def image_bytes(self):
        return base64.b64decode(self.image_b64)


def _parse(provider, response):
    payload = response.json()
    if provider.style == "token_image":
        data = payload["response"]["data"]
        return {"image": data["image"], "token": data["token"]}
    if provider.style == "token_cookies":
        data = payload["Data"]
        return {"image": data["Captcha"], "token": data["CaptchaKey"], "cookies": dict(response.cookies)}
    return {"image": payload["captchaByteData"], "hash": payload.get("hashedCaptcha"), "salt": payload.get("salt")}


async def fetch_captcha(platform, client=None):
    """Get a fresh captcha from the provider."""
    provider = PROVIDERS[platform]
    started = time.perf_counter()
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        response = await client.get(provider.url, headers=provider.headers, timeout=config.PROVIDER_TIMEOUT)
        response.raise_for_status()
        parsed = _parse(provider, response)
    except httpx.HTTPStatusError as e:
        raise ProviderError(f"{platform} answered HTTP {e.response.status_code}") from e
    except httpx.RequestError as e:
        raise ProviderError(f"{platform} unreachable: {e.__class__.__name__}") from e
    except (KeyError, ValueError, TypeError) as e:
        raise ProviderError(f"{platform} returned an unexpected response") from e
    finally:
        if owns_client:
            await client.aclose()
    if not parsed.get("image"):
        raise ProviderError(f"{platform} returned no image")
    return Captcha(platform=platform, fetch_ms=round((time.perf_counter() - started) * 1000), **{
        "image_b64": parsed["image"], "token": parsed.get("token"), "hash": parsed.get("hash"),
        "salt": parsed.get("salt"), "cookies": parsed.get("cookies", {}),
    })
