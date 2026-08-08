"""Tests for the crop/recompose maths — no network, no API key needed.

The property that matters: everything outside the (feathered) selection must
come back byte-for-byte identical, and the result must be the original size.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.compose import (  # noqa: E402
    auto_feather,
    clamp_box,
    edit_region,
    plan_crop,
    rect_mask,
)


def sample_photo(size=(1600, 1200)) -> Image.Image:
    img = Image.new("RGB", size, (40, 90, 140))
    draw = ImageDraw.Draw(img)
    for x in range(0, size[0], 40):
        draw.line([(x, 0), (x, size[1])], fill=(200, 170, 120), width=3)
    draw.ellipse([200, 150, 500, 450], fill=(230, 210, 190))  # stand-in for a face
    return img


def solid(colour):
    """A fake model that always returns a flat colour of the requested size."""

    def render(crop, mask):
        return Image.new("RGB", crop.size, colour)

    return render


def test_clamp_box_normalises_and_clips():
    assert clamp_box((-50, -10, 100, 80), (200, 200)) == (0, 0, 100, 80)
    assert clamp_box((150, 90, 50, 10), (200, 200)) == (50, 10, 150, 90)  # reversed drag
    assert clamp_box((10, 10, 10, 10), (200, 200)) == (10, 10, 11, 11)  # never zero-sized


def test_plan_adds_context_and_bounds_send_size():
    plan = plan_crop((400, 400, 600, 600), (1600, 1200), context_pct=25, max_send_px=1024)
    assert plan.selection == (400, 400, 600, 600)
    assert plan.context == (350, 350, 650, 650)  # 25% of 200px on each side
    assert max(plan.send_size) <= 1024
    assert max(plan.send_size) >= 512  # small crops get upscaled for detail


def test_plan_clips_context_at_the_edge():
    plan = plan_crop((0, 0, 200, 200), (1600, 1200), context_pct=50)
    assert plan.context[0] == 0 and plan.context[1] == 0
    assert plan.context[2] == 300 and plan.context[3] == 300


def test_send_size_is_capped_for_huge_crops():
    plan = plan_crop((0, 0, 6000, 4000), (6000, 4000), context_pct=10, max_send_px=1536)
    assert max(plan.send_size) == 1536


def test_untouched_pixels_are_identical():
    base = sample_photo()
    result, plan = edit_region(base, (900, 700, 1200, 1000), solid((255, 0, 0)), feather=0)

    assert result.size == base.size

    # A generous margin around the context crop must be pixel-identical.
    left, top, right, bottom = plan.context
    for box in [
        (0, 0, base.width, max(0, top - 4)),
        (0, min(base.height, bottom + 4), base.width, base.height),
        (0, 0, max(0, left - 4), base.height),
        (min(base.width, right + 4), 0, base.width, base.height),
    ]:
        if box[2] > box[0] and box[3] > box[1]:
            assert result.crop(box).tobytes() == base.crop(box).tobytes()


def test_the_face_area_is_untouched_when_editing_elsewhere():
    base = sample_photo()
    face = (200, 150, 500, 450)
    result, _ = edit_region(base, (1100, 800, 1400, 1050), solid((0, 255, 0)))
    assert result.crop(face).tobytes() == base.crop(face).tobytes()


def test_selection_centre_actually_changes():
    base = sample_photo()
    result, _ = edit_region(base, (600, 500, 900, 800), solid((255, 0, 255)), tone_match=0)
    assert result.getpixel((750, 650)) != base.getpixel((750, 650))


def test_feather_is_gradual_not_a_hard_edge():
    base = Image.new("RGB", (600, 600), (0, 0, 0))
    result, _ = edit_region(base, (200, 200, 400, 400), solid((255, 255, 255)), feather=12, tone_match=0)
    # Walking outward across the boundary, values should decay rather than jump.
    values = [result.getpixel((x, 300))[0] for x in range(190, 215)]
    assert values[0] < values[-1]
    assert max(b - a for a, b in zip(values, values[1:])) < 200


def test_painted_mask_limits_the_change_to_the_painted_shape():
    base = Image.new("RGB", (800, 800), (10, 10, 10))
    mask = Image.new("L", base.size, 0)
    ImageDraw.Draw(mask).ellipse([300, 300, 500, 500], fill=255)

    result, _ = edit_region(
        base, (300, 300, 500, 500), solid((255, 255, 255)), mask=mask, feather=0, tone_match=0
    )
    assert result.getpixel((400, 400))[0] > 200  # inside the circle: changed
    assert result.getpixel((305, 305)) == (10, 10, 10)  # corner of the box, outside it: not


def test_tone_match_pulls_a_drifted_crop_back():
    base = Image.new("RGB", (800, 800), (120, 120, 120))

    def brighter(crop, mask):
        # Simulate a model that returns the same content but 40 levels brighter.
        return Image.new("RGB", crop.size, (160, 160, 160))

    corrected, _ = edit_region(base, (300, 300, 500, 500), brighter, feather=0, tone_match=1.0)
    raw, _ = edit_region(base, (300, 300, 500, 500), brighter, feather=0, tone_match=0.0)

    centre = (400, 400)
    assert abs(corrected.getpixel(centre)[0] - 120) < abs(raw.getpixel(centre)[0] - 120)


def test_tone_match_does_not_cancel_an_intentional_colour_change():
    """Asking for red in a blue scene must still give red, not corrected-to-blue."""
    base = Image.new("RGB", (800, 800), (30, 60, 200))

    def red(crop, mask):
        out = crop.copy()
        ImageDraw.Draw(out).rectangle([0, 0, out.width, out.height], fill=(30, 60, 200))
        # Only the middle — the part the user selected — turns red; the context
        # ring keeps the original colour, the way a real model behaves.
        inset = out.width // 5
        ImageDraw.Draw(out).rectangle([inset, inset, out.width - inset, out.height - inset], fill=(210, 40, 40))
        return out

    result, _ = edit_region(base, (300, 300, 500, 500), red, feather=0, tone_match=1.0)
    r, g, b = result.getpixel((400, 400))
    assert r > 150 and b < 90


def test_auto_feather_scales_with_selection_size():
    assert auto_feather((0, 0, 100, 100), None) == 2  # clamped floor
    assert auto_feather((0, 0, 2000, 2000), None) == 30
    assert auto_feather((0, 0, 2000, 2000), 5) == 5  # explicit wins


def test_rect_mask_covers_exactly_the_box():
    mask = rect_mask((100, 100), (10, 10, 20, 20))
    assert mask.getpixel((15, 15)) == 255
    assert mask.getpixel((9, 15)) == 0
    assert mask.getpixel((20, 20)) == 0  # right/bottom are exclusive


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
