"""Selective AI photo editor — HTTP API.

Full-resolution images live here on the server for the life of the session; the
browser only ever handles a downscaled preview. That way a 48 MP photo isn't
shuttled back and forth on every edit, and the final download is composed from
the original pixels rather than from anything the browser re-encoded.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import requests
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import providers
from .compose import Box, edit_region, load_image

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
PREVIEW_MAX = 1400
MAX_SESSIONS = 24
SESSION_TTL_SECONDS = 6 * 60 * 60

# Decoded images are held in memory at full resolution — a 48 MP photo is about
# 144 MB as RGB, and each edit adds another version. Left unbounded that OOMs a
# small instance, so both the history depth and the total footprint are capped.
# Defaults suit a roomy laptop; a 512 MB host wants MEMORY_BUDGET_MB=300 or so.
MAX_VERSIONS = int(os.environ.get("MAX_VERSIONS", "8"))
MEMORY_BUDGET_BYTES = int(os.environ.get("MEMORY_BUDGET_MB", "1500")) * 1024 * 1024
MAX_INPUT_PIXELS = int(os.environ.get("MAX_INPUT_MP", "80")) * 1_000_000

app = FastAPI(title="Selective AI Photo Editor")


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Gate the whole app behind a password when APP_PASSWORD is set.

    Anywhere this is reachable by more than you — a LAN, a deployed preview —
    an open page means anyone who finds it can spend your API key. Basic auth
    is the one scheme Safari and every other browser prompt for natively, with
    no login page to build or session cookie to get wrong.
    """
    expected = os.environ.get("APP_PASSWORD", "").strip()
    if not expected or request.url.path == "/healthz":
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            _, _, supplied = base64.b64decode(header[6:]).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            supplied = ""
        if secrets.compare_digest(supplied, expected):
            return await call_next(request)

    return PlainTextResponse(
        "Password required.",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Photo editor"'},
    )


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated, so platform health checks don't need the password."""
    return {"ok": True}


@dataclass
class Session:
    """One uploaded photo plus every version produced from it."""

    id: str
    versions: list[Image.Image]
    source_format: str
    source_name: str
    created: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)

    @property
    def current(self) -> Image.Image:
        return self.versions[-1]

    @property
    def bytes_held(self) -> int:
        return sum(img.width * img.height * 3 for img in self.versions)

    def add_version(self, img: Image.Image) -> None:
        """Append an edit, discarding the oldest one if the history is full.

        The original is always kept at index 0 — "back to original" has to work
        no matter how many edits you've stacked up — so the oldest *edit* is
        what gets dropped.
        """
        self.versions.append(img)
        while len(self.versions) > MAX_VERSIONS:
            del self.versions[1]
        self.touched = time.time()


_sessions: dict[str, Session] = {}
_lock = Lock()


def _evict() -> None:
    """Drop stale sessions, then the least-recently-used until we fit."""
    now = time.time()
    for sid in [s for s, sess in _sessions.items() if now - sess.touched > SESSION_TTL_SECONDS]:
        _sessions.pop(sid, None)

    by_age = sorted(_sessions.values(), key=lambda s: s.touched)
    while len(_sessions) > MAX_SESSIONS and by_age:
        _sessions.pop(by_age.pop(0).id, None)

    held = sum(s.bytes_held for s in _sessions.values())
    # Never evict the newest session: it's the one being worked on, and dropping
    # it would fail the very request that triggered this.
    while held > MEMORY_BUDGET_BYTES and len(by_age) > 1:
        victim = by_age.pop(0)
        held -= victim.bytes_held
        _sessions.pop(victim.id, None)


def _get(session_id: str) -> Session:
    with _lock:
        session = _sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "Image not found — it may have expired. Upload it again.")
        session.touched = time.time()
        return session


def _preview_bytes(img: Image.Image) -> bytes:
    preview = img.copy()
    preview.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.LANCZOS)
    buf = io.BytesIO()
    preview.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _state(session: Session) -> dict:
    img = session.current
    return {
        "id": session.id,
        "width": img.width,
        "height": img.height,
        "version": len(session.versions) - 1,
        "versions": len(session.versions),
        "name": session.source_name,
        "format": session.source_format,
        "preview_url": f"/api/images/{session.id}/preview?v={len(session.versions) - 1}",
    }


@app.get("/api/providers")
def list_providers() -> dict:
    return {"providers": providers.available()}


@app.post("/api/images")
async def upload(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty upload.")
    try:
        img = load_image(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't read that image: {exc}") from exc

    if img.width * img.height > MAX_INPUT_PIXELS:
        raise HTTPException(
            413,
            f"That photo is {img.width * img.height / 1e6:.0f} MP; this server is "
            f"configured to accept up to {MAX_INPUT_PIXELS / 1e6:.0f} MP. "
            "Raise MAX_INPUT_MP if the machine has the memory for it.",
        )

    fmt = (Image.open(io.BytesIO(raw)).format or "PNG").upper()
    session = Session(
        id=uuid.uuid4().hex[:12],
        versions=[img],
        source_format=fmt,
        source_name=file.filename or "photo",
    )
    with _lock:
        _sessions[session.id] = session
        _evict()
    return _state(session)


@app.get("/api/images/{session_id}/preview")
def preview(session_id: str, v: int | None = None) -> Response:
    session = _get(session_id)
    index = len(session.versions) - 1 if v is None else v
    if not 0 <= index < len(session.versions):
        raise HTTPException(404, "No such version.")
    return Response(
        _preview_bytes(session.versions[index]),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/images/{session_id}/undo")
def undo(session_id: str) -> dict:
    session = _get(session_id)
    with _lock:
        if len(session.versions) > 1:
            session.versions.pop()
    return _state(session)


@app.post("/api/images/{session_id}/revert")
def revert(session_id: str, body: dict = Body(default={})) -> dict:
    """Jump back to any earlier version without losing the original."""
    session = _get(session_id)
    index = int(body.get("version", 0))
    if not 0 <= index < len(session.versions):
        raise HTTPException(400, "No such version.")
    with _lock:
        session.versions = session.versions[: index + 1]
    return _state(session)


def _parse_selection(raw: str | None, size: tuple[int, int]) -> Box:
    if not raw:
        raise HTTPException(400, "Region mode needs a selection.")
    try:
        data = json.loads(raw)
        box = (float(data["left"]), float(data["top"]), float(data["right"]), float(data["bottom"]))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Bad selection: {exc}") from exc
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        raise HTTPException(400, "That selection is too small — drag a larger area.")
    return box  # clamped downstream by plan_crop


@app.post("/api/images/{session_id}/edit")
async def edit(
    session_id: str,
    prompt: str = Form(...),
    mode: str = Form("region"),
    provider: str = Form("gemini"),
    model: str | None = Form(None),
    api_key: str | None = Form(None),
    selection: str | None = Form(None),
    context_pct: float = Form(25.0),
    max_send_px: int = Form(1536),
    feather: float | None = Form(None),
    tone_match: float = Form(1.0),
    mask: UploadFile | None = File(None),
) -> dict:
    session = _get(session_id)
    base = session.current
    if not prompt.strip():
        raise HTTPException(400, "Describe the change you want.")

    def call(image: Image.Image, image_mask: Image.Image | None) -> Image.Image:
        try:
            return providers.render(
                provider,
                image,
                image_mask,
                prompt,
                model=model,
                api_key=api_key,
            )
        except providers.ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except requests.RequestException as exc:  # pragma: no cover - network dependent
            raise HTTPException(504, f"Couldn't reach the model: {exc}") from exc

    started = time.time()

    if mode == "whole":
        result = call(base, None)
        # Even a whole-image edit is returned at the original resolution, so the
        # file you download matches the file you uploaded.
        if result.size != base.size:
            result = result.resize(base.size, Image.LANCZOS)
        info = {"mode": "whole"}
    else:
        box = _parse_selection(selection, base.size)
        user_mask: Image.Image | None = None
        if mask is not None:
            raw = await mask.read()
            if raw:
                painted = Image.open(io.BytesIO(raw)).convert("L")
                if painted.size != base.size:
                    painted = painted.resize(base.size, Image.LANCZOS)
                user_mask = painted
        result, plan = edit_region(
            base,
            box,
            call,
            mask=user_mask,
            context_pct=context_pct,
            max_send_px=max_send_px,
            feather=feather,
            tone_match=tone_match,
        )
        info = {
            "mode": "region",
            "selection": list(plan.selection),
            "context": list(plan.context),
            "sent_size": list(plan.send_size),
            "megapixels_sent": round(plan.send_size[0] * plan.send_size[1] / 1e6, 2),
            "megapixels_total": round(base.width * base.height / 1e6, 2),
        }

    with _lock:
        session.add_version(result)
        _evict()

    state = _state(session)
    state["edit"] = {**info, "seconds": round(time.time() - started, 1), "prompt": prompt.strip()}
    return state


@app.get("/api/images/{session_id}/download")
def download(session_id: str, format: str = "original", quality: int = 95, v: int | None = None):
    session = _get(session_id)
    index = len(session.versions) - 1 if v is None else v
    if not 0 <= index < len(session.versions):
        raise HTTPException(404, "No such version.")
    img = session.versions[index]

    fmt = session.source_format if format == "original" else format.upper()
    if fmt in {"JPG", "JPEG"}:
        fmt, ext, params = "JPEG", "jpg", {"quality": int(quality), "subsampling": 0, "optimize": True}
    elif fmt == "WEBP":
        fmt, ext, params = "WEBP", "webp", {"quality": int(quality), "method": 6}
    else:
        fmt, ext, params = "PNG", "png", {"optimize": True}

    buf = io.BytesIO()
    img.save(buf, format=fmt, **params)
    stem = Path(session.source_name).stem or "photo"
    return Response(
        buf.getvalue(),
        media_type=f"image/{ext}",
        headers={"Content-Disposition": f'attachment; filename="{stem}-edited.{ext}"'},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
