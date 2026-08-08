"""Image-editing model backends.

Each provider takes an image (and optionally a mask) plus a prompt, and returns
an edited image of the same subject. They are deliberately interchangeable: the
crop/recompose pipeline in ``compose.py`` doesn't know or care which one runs.

Keys are never stored server-side. They come from the environment, or are sent
per-request by the browser (kept in localStorage on your machine only).
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass

import requests
from PIL import Image

TIMEOUT = 180


class ProviderError(RuntimeError):
    """A provider refused or failed. The message is shown to the user."""


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    default_model: str
    models: tuple[str, ...]
    supports_mask: bool
    env_var: str
    docs: str


PROVIDERS: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo(
        id="gemini",
        label="Google Gemini (Nano Banana)",
        default_model="gemini-3-pro-image-preview",
        models=(
            "gemini-3-pro-image-preview",
            "gemini-3.1-flash-image-preview",
            "gemini-2.5-flash-image",
        ),
        supports_mask=False,
        env_var="GEMINI_API_KEY",
        docs="https://ai.google.dev/gemini-api/docs/image-generation",
    ),
    "openai": ProviderInfo(
        id="openai",
        label="OpenAI Images",
        default_model="gpt-image-1",
        models=("gpt-image-1",),
        supports_mask=True,
        env_var="OPENAI_API_KEY",
        docs="https://platform.openai.com/docs/api-reference/images/createEdit",
    ),
    "stability": ProviderInfo(
        id="stability",
        label="Stability AI (inpaint)",
        default_model="core",
        models=("core",),
        supports_mask=True,
        env_var="STABILITY_API_KEY",
        docs="https://platform.stability.ai/docs/api-reference",
    ),
}


def resolve_key(provider: str, supplied: str | None) -> str:
    key = (supplied or "").strip() or os.environ.get(PROVIDERS[provider].env_var, "").strip()
    if not key:
        raise ProviderError(
            f"No API key for {PROVIDERS[provider].label}. "
            f"Set {PROVIDERS[provider].env_var} or paste a key in the settings panel."
        )
    return key


def _to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _decode(data: bytes) -> Image.Image:
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as-is
        raise ProviderError(f"Model returned something that isn't an image: {exc}") from exc


def _openai_mask(mask: Image.Image, size: tuple[int, int]) -> bytes:
    """OpenAI edits the *transparent* pixels, so invert our white-means-edit mask."""
    mask = mask.convert("L").resize(size, Image.LANCZOS)
    rgba = Image.new("RGBA", size, (0, 0, 0, 255))
    alpha = mask.point(lambda v: 0 if v > 127 else 255)
    rgba.putalpha(alpha)
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return buf.getvalue()


def _nearest_openai_size(size: tuple[int, int]) -> str:
    w, h = size
    ratio = w / h
    if ratio > 1.2:
        return "1536x1024"
    if ratio < 0.83:
        return "1024x1536"
    return "1024x1024"


def run_gemini(image: Image.Image, mask: Image.Image | None, prompt: str, model: str, key: str) -> Image.Image:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": base64.b64encode(_to_png_bytes(image)).decode(),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ProviderError(f"Gemini {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    for candidate in body.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            blob = part.get("inline_data") or part.get("inlineData")
            if blob and blob.get("data"):
                return _decode(base64.b64decode(blob["data"]))
    raise ProviderError(f"Gemini returned no image (it may have refused): {str(body)[:400]}")


def run_openai(image: Image.Image, mask: Image.Image | None, prompt: str, model: str, key: str) -> Image.Image:
    files = {
        "image": ("image.png", _to_png_bytes(image), "image/png"),
    }
    if mask is not None:
        files["mask"] = ("mask.png", _openai_mask(mask, image.size), "image/png")
    data = {
        "model": model,
        "prompt": prompt,
        "size": _nearest_openai_size(image.size),
        "n": "1",
    }
    resp = requests.post(
        "https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {key}"},
        files=files,
        data=data,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ProviderError(f"OpenAI {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    items = body.get("data") or []
    if not items:
        raise ProviderError(f"OpenAI returned no image: {str(body)[:400]}")
    first = items[0]
    if first.get("b64_json"):
        return _decode(base64.b64decode(first["b64_json"]))
    if first.get("url"):
        return _decode(requests.get(first["url"], timeout=TIMEOUT).content)
    raise ProviderError("OpenAI response contained neither b64_json nor url.")


def run_stability(image: Image.Image, mask: Image.Image | None, prompt: str, model: str, key: str) -> Image.Image:
    files = {"image": ("image.png", _to_png_bytes(image), "image/png")}
    data = {"prompt": prompt, "output_format": "png"}
    if mask is not None:
        # Stability inpaints the white pixels, which matches our convention.
        files["mask"] = ("mask.png", _to_png_bytes(mask.convert("L").resize(image.size, Image.LANCZOS)), "image/png")
        endpoint = "https://api.stability.ai/v2beta/stable-image/edit/inpaint"
    else:
        endpoint = "https://api.stability.ai/v2beta/stable-image/edit/search-and-replace"
        data["search_prompt"] = prompt

    resp = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {key}", "Accept": "image/*"},
        files=files,
        data=data,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ProviderError(f"Stability {resp.status_code}: {resp.text[:400]}")
    return _decode(resp.content)


_RUNNERS = {"gemini": run_gemini, "openai": run_openai, "stability": run_stability}


def render(
    provider: str,
    image: Image.Image,
    mask: Image.Image | None,
    prompt: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> Image.Image:
    """Send one crop (or one whole image) to a provider and get the edit back."""
    if provider not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{provider}'.")
    info = PROVIDERS[provider]
    if not info.supports_mask:
        mask = None
    key = resolve_key(provider, api_key)
    return _RUNNERS[provider](image, mask, prompt.strip(), model or info.default_model, key)


def available() -> list[dict]:
    """Provider metadata for the UI, including which ones already have a key."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "models": list(p.models),
            "default_model": p.default_model,
            "supports_mask": p.supports_mask,
            "env_var": p.env_var,
            "docs": p.docs,
            "key_in_env": bool(os.environ.get(p.env_var, "").strip()),
        }
        for p in PROVIDERS.values()
    ]
