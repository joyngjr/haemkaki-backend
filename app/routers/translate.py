"""Machine translation for the Medical ID card, proxied to LibreTranslate.

The browser never calls LibreTranslate itself: the public instance needs an
API key, and a key shipped in the frontend bundle is a key anyone can read.

The card sends its fixed English copy — section titles, field labels and the
diagnosis and severity vocabulary — never the person's own details, so nothing
identifying leaves for the third party. That copy is the same for everyone, so
each translation is kept in memory and a language costs one upstream call per
process, not one per view.
"""

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/translate", tags=["translate"])

# LibreTranslate's own codes, restricted to the languages of the countries the
# Find Medical Help map covers that it can translate into. It has no Tamil,
# Burmese, Khmer or Lao.
TargetLanguage = Literal["zh-Hans", "zh-Hant", "ms", "id", "th", "vi", "tl", "ja", "ko", "hi"]

# The card's copy is a few dozen strings; anything much bigger is not the card.
_MAX_TEXTS = 100
_MAX_TEXT_LENGTH = 200
_CACHE_LIMIT = 5_000

_cache: dict[tuple[str, str], str] = {}


class TranslateRequest(BaseModel):
    target: TargetLanguage
    texts: list[str] = Field(min_length=1, max_length=_MAX_TEXTS)


class TranslateResponse(BaseModel):
    target: TargetLanguage
    # Same order as the request's `texts`.
    translations: list[str]


async def _libretranslate(texts: list[str], target: str) -> list[str]:
    settings = get_settings()
    payload: dict[str, object] = {"q": texts, "source": "en", "target": target, "format": "text"}
    if settings.libretranslate_api_key:
        payload["api_key"] = settings.libretranslate_api_key

    try:
        # Long enough for a self-hosted instance to wake from Railway's sleep
        # and load its models before answering.
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{settings.libretranslate_url.rstrip('/')}/translate", json=payload
            )
    except httpx.HTTPError as exc:
        logger.warning("LibreTranslate unreachable: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The translation service could not be reached"
        ) from exc

    try:
        body = response.json()
    except ValueError:
        body = None
    if response.is_error:
        # LibreTranslate explains itself in `error` — most often a missing or
        # spent API key — which is worth passing on rather than a bare 502.
        reason = body.get("error") if isinstance(body, dict) else None
        logger.warning("LibreTranslate answered %s: %s", response.status_code, reason)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Translation failed: {reason}" if reason else "Translation failed",
        )

    translated = body.get("translatedText") if isinstance(body, dict) else None
    if not isinstance(translated, list) or len(translated) != len(texts):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "The translation service sent back an unexpected answer"
        )
    return [str(text) for text in translated]


@router.post("", response_model=TranslateResponse)
async def translate(payload: TranslateRequest) -> TranslateResponse:
    """Translate English strings into `target`, in order."""
    for text in payload.texts:
        if len(text) > _MAX_TEXT_LENGTH:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Each text must be at most {_MAX_TEXT_LENGTH} characters",
            )

    found = {t: _cache[(payload.target, t)] for t in payload.texts if (payload.target, t) in _cache}
    missing = [t for t in dict.fromkeys(payload.texts) if t not in found]
    if missing:
        translated = await _libretranslate(missing, payload.target)
        fresh = dict(zip(missing, translated, strict=True))
        found.update(fresh)
        if len(_cache) + len(fresh) > _CACHE_LIMIT:
            _cache.clear()
        _cache.update({(payload.target, t): out for t, out in fresh.items()})

    return TranslateResponse(target=payload.target, translations=[found[t] for t in payload.texts])
