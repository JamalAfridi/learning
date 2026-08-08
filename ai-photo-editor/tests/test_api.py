"""End-to-end API tests with the model provider stubbed out.

These exercise upload -> edit -> undo -> download without touching the network,
so they run offline and without an API key.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import app as app_module, providers  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    def fake_render(provider, image, mask, prompt, *, model=None, api_key=None):
        return Image.new("RGB", image.size, (255, 0, 0))

    monkeypatch.setattr(providers, "render", fake_render)
    app_module._sessions.clear()
    return TestClient(app_module.app)


def photo_bytes(size=(1200, 900)) -> bytes:
    img = Image.new("RGB", size, (30, 80, 130))
    ImageDraw.Draw(img).ellipse([100, 100, 400, 400], fill=(240, 220, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def upload(client) -> dict:
    res = client.post("/api/images", files={"file": ("photo.jpg", photo_bytes(), "image/jpeg")})
    assert res.status_code == 200, res.text
    return res.json()


def test_upload_reports_original_dimensions(client):
    state = upload(client)
    assert (state["width"], state["height"]) == (1200, 900)
    assert state["version"] == 0


def test_region_edit_keeps_size_and_reports_what_was_sent(client):
    state = upload(client)
    res = client.post(
        f"/api/images/{state['id']}/edit",
        data={
            "prompt": "white sneakers",
            "mode": "region",
            "provider": "gemini",
            "selection": json.dumps({"left": 700, "top": 500, "right": 950, "bottom": 750}),
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["width"], body["height"]) == (1200, 900)
    assert body["version"] == 1
    assert body["edit"]["mode"] == "region"
    assert body["edit"]["megapixels_sent"] < body["edit"]["megapixels_total"]


def test_download_is_full_resolution_and_face_area_survives(client):
    state = upload(client)
    client.post(
        f"/api/images/{state['id']}/edit",
        data={
            "prompt": "white sneakers",
            "mode": "region",
            "selection": json.dumps({"left": 800, "top": 600, "right": 1000, "bottom": 800}),
        },
    )
    res = client.get(f"/api/images/{state['id']}/download?format=png")
    assert res.status_code == 200
    out = Image.open(io.BytesIO(res.content))
    assert out.size == (1200, 900)
    assert out.getpixel((250, 250))[0] > 200  # the "face" ellipse is still light, not red


def test_whole_image_mode_replaces_everything_but_keeps_dimensions(client):
    state = upload(client)
    res = client.post(
        f"/api/images/{state['id']}/edit",
        data={"prompt": "make it a painting", "mode": "whole"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["edit"]["mode"] == "whole"
    out = Image.open(io.BytesIO(client.get(f"/api/images/{state['id']}/download?format=png").content))
    assert out.size == (1200, 900)
    assert out.getpixel((250, 250)) == (255, 0, 0)


def test_undo_and_revert_walk_the_history(client):
    state = upload(client)
    for _ in range(2):
        client.post(
            f"/api/images/{state['id']}/edit",
            data={"prompt": "x", "mode": "region", "selection": json.dumps({"left": 10, "top": 10, "right": 200, "bottom": 200})},
        )
    assert client.post(f"/api/images/{state['id']}/undo").json()["version"] == 1
    assert client.post(f"/api/images/{state['id']}/revert", json={"version": 0}).json()["version"] == 0


def test_bad_input_is_rejected_clearly(client):
    state = upload(client)
    missing = client.post(f"/api/images/{state['id']}/edit", data={"prompt": "x", "mode": "region"})
    assert missing.status_code == 400

    tiny = client.post(
        f"/api/images/{state['id']}/edit",
        data={"prompt": "x", "mode": "region", "selection": json.dumps({"left": 10, "top": 10, "right": 12, "bottom": 12})},
    )
    assert tiny.status_code == 400

    blank = client.post(
        f"/api/images/{state['id']}/edit",
        data={"prompt": "   ", "mode": "whole"},
    )
    assert blank.status_code == 400

    assert client.get("/api/images/nope/preview").status_code == 404


def test_missing_key_surfaces_as_a_readable_error(monkeypatch):
    """The one path that is not stubbed: no credentials configured."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    app_module._sessions.clear()
    client = TestClient(app_module.app)
    state = upload(client)
    res = client.post(
        f"/api/images/{state['id']}/edit",
        data={"prompt": "x", "mode": "region", "selection": json.dumps({"left": 10, "top": 10, "right": 300, "bottom": 300})},
    )
    assert res.status_code == 502
    assert "GEMINI_API_KEY" in res.json()["detail"]


def test_providers_endpoint_lists_backends(client):
    body = client.get("/api/providers").json()
    ids = {p["id"] for p in body["providers"]}
    assert {"gemini", "openai", "stability"} <= ids


def test_history_is_capped_but_always_keeps_the_original(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_VERSIONS", 3)
    state = upload(client)
    first = Image.open(io.BytesIO(client.get(f"/api/images/{state['id']}/preview?v=0").content))

    for _ in range(5):
        client.post(
            f"/api/images/{state['id']}/edit",
            data={"prompt": "x", "mode": "whole"},
        )

    session = app_module._sessions[state["id"]]
    assert len(session.versions) == 3
    # Version 0 is still the upload, not an edit that scrolled into its place.
    kept = Image.open(io.BytesIO(client.get(f"/api/images/{state['id']}/preview?v=0").content))
    assert kept.tobytes() == first.tobytes()


def test_oversized_upload_is_refused_with_a_useful_message(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_INPUT_PIXELS", 100_000)
    res = client.post("/api/images", files={"file": ("big.jpg", photo_bytes((1200, 900)), "image/jpeg")})
    assert res.status_code == 413
    assert "MP" in res.json()["detail"]


def test_memory_budget_evicts_old_sessions_but_keeps_the_newest(client, monkeypatch):
    monkeypatch.setattr(app_module, "MEMORY_BUDGET_BYTES", 1200 * 900 * 3 * 2)
    ids = [upload(client)["id"] for _ in range(4)]
    assert ids[-1] in app_module._sessions  # the one in use survives
    assert len(app_module._sessions) <= 2


def test_no_password_configured_means_no_gate(client, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert client.get("/").status_code == 200


def test_password_gate_blocks_and_admits(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2")

    anonymous = client.get("/")
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"].startswith("Basic")

    assert client.get("/", auth=("any", "wrong")).status_code == 401
    assert client.get("/", auth=("any", "hunter2")).status_code == 200

    # The gate covers the API too, not just the page.
    assert client.get("/api/providers").status_code == 401
    assert client.get("/api/providers", auth=("any", "hunter2")).status_code == 200


def test_malformed_auth_header_is_rejected_not_crashed(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    assert client.get("/", headers={"Authorization": "Basic not-base64!!"}).status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer hunter2"}).status_code == 401


def test_health_check_stays_open_for_platform_probes(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    res = client.get("/healthz")
    assert res.status_code == 200 and res.json() == {"ok": True}
