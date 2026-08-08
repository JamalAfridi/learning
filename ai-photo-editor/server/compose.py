"""Crop / edit / recompose pipeline.

The point of this module: never hand the whole photo to a generative model when
only a small part of it needs to change. Instead we

  1. take the user's selection (rectangle or painted mask),
  2. expand it by a context margin so the model can see what surrounds it,
  3. send only that crop to the model,
  4. resize the model's answer back to the exact pixel size of the crop,
  5. correct any global tone shift the model introduced, and
  6. paste it back through a feathered mask so the seam is invisible.

Everything outside the feathered mask is byte-for-byte the original pixels, so
faces, hands and text elsewhere in the photo cannot be mangled.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageOps

Box = tuple[int, int, int, int]  # (left, top, right, bottom), right/bottom exclusive


@dataclass(frozen=True)
class CropPlan:
    """How a selection maps onto the image we actually send to the model."""

    selection: Box  # what the user asked to change, in original pixels
    context: Box  # selection + margin, what we send to the model
    send_size: tuple[int, int]  # size the crop is resampled to for the model

    @property
    def context_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.context
        return right - left, bottom - top


def load_image(fp) -> Image.Image:
    """Open an image, honouring EXIF orientation, as RGB."""
    img = Image.open(fp)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def clamp_box(box: Box, size: tuple[int, int]) -> Box:
    """Clip a box to the image and normalise it to integers, left<right."""
    w, h = size
    left, top, right, bottom = (int(round(v)) for v in box)
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    left = max(0, min(left, w))
    top = max(0, min(top, h))
    right = max(0, min(right, w))
    bottom = max(0, min(bottom, h))
    if right - left < 1:
        left = max(0, min(left, w - 1))
        right = left + 1
    if bottom - top < 1:
        top = max(0, min(top, h - 1))
        bottom = top + 1
    return left, top, right, bottom


def plan_crop(
    selection: Box,
    image_size: tuple[int, int],
    *,
    context_pct: float = 25.0,
    max_send_px: int = 1536,
    min_send_px: int = 512,
) -> CropPlan:
    """Expand the selection by a context margin and decide the send resolution.

    ``context_pct`` is a percentage of the selection's own width/height, so a
    small selection gets a proportionally small — but never zero — margin.
    """
    selection = clamp_box(selection, image_size)
    left, top, right, bottom = selection
    sel_w, sel_h = right - left, bottom - top

    margin_x = max(8, int(sel_w * context_pct / 100.0))
    margin_y = max(8, int(sel_h * context_pct / 100.0))
    context = clamp_box(
        (left - margin_x, top - margin_y, right + margin_x, bottom + margin_y),
        image_size,
    )

    ctx_w = context[2] - context[0]
    ctx_h = context[3] - context[1]
    longest = max(ctx_w, ctx_h)

    # Upscale small crops so the model has enough pixels to work with, and
    # downscale huge ones so we stay inside the model's input limits.
    if longest > max_send_px:
        scale = max_send_px / longest
    elif longest < min_send_px:
        scale = min_send_px / longest
    else:
        scale = 1.0
    send = (max(1, round(ctx_w * scale)), max(1, round(ctx_h * scale)))

    return CropPlan(selection=selection, context=context, send_size=send)


def rect_mask(size: tuple[int, int], box: Box) -> Image.Image:
    """A full-size 8-bit mask that is white inside ``box`` and black outside."""
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle([box[0], box[1], box[2] - 1, box[3] - 1], fill=255)
    return mask


def feather_mask(mask: Image.Image, radius: float) -> Image.Image:
    """Soften a mask's edge so the composite has no visible seam."""
    if radius <= 0:
        return mask
    return mask.filter(ImageFilter.GaussianBlur(radius))


def auto_feather(selection: Box, requested: float | None) -> float:
    """Default the feather to ~1.5% of the selection's short side."""
    if requested is not None:
        return max(0.0, float(requested))
    short = min(selection[2] - selection[0], selection[3] - selection[1])
    return float(min(48, max(2, round(short * 0.015))))


def _channel_stats(img: Image.Image, mask: Image.Image) -> list[tuple[float, float]]:
    """Per-channel (mean, stddev) of ``img`` over the white part of ``mask``."""
    stats: list[tuple[float, float]] = []
    for channel in img.split():
        hist = channel.histogram(mask)
        total = sum(hist)
        if total == 0:
            stats.append((0.0, 0.0))
            continue
        mean = sum(i * n for i, n in enumerate(hist)) / total
        var = sum((i - mean) ** 2 * n for i, n in enumerate(hist)) / total
        stats.append((mean, var**0.5))
    return stats


def match_tone(
    edited: Image.Image,
    reference: Image.Image,
    ring: Image.Image,
    *,
    strength: float = 1.0,
) -> Image.Image:
    """Pull ``edited`` back towards ``reference`` using the untouched ring.

    Generative models often return a crop that is a shade brighter, cooler or
    more contrasty than the original. The ring is the context margin, which is
    meant to be unchanged, so any difference there is model drift — we measure
    it and undo it across the whole crop.
    """
    if strength <= 0:
        return edited

    src = _channel_stats(edited, ring)
    dst = _channel_stats(reference, ring)

    out_channels = []
    for channel, (s_mean, s_std), (d_mean, d_std) in zip(edited.split(), src, dst):
        gain = (d_std / s_std) if s_std > 1e-3 else 1.0
        gain = min(1.6, max(0.625, gain))
        offset = d_mean - gain * s_mean
        # Both corrections are deliberately bounded. This is here to undo model
        # drift, not to fight the edit: without a cap, asking for red shoes in a
        # blue scene would have the ring's blue statistics drag them back.
        offset = min(32.0, max(-32.0, offset))
        gain = 1.0 + (gain - 1.0) * strength
        offset *= strength
        out_channels.append(channel.point(lambda v, g=gain, o=offset: int(max(0, min(255, v * g + o)))))
    return Image.merge("RGB", out_channels)


def recompose(
    base: Image.Image,
    edited_crop: Image.Image,
    plan: CropPlan,
    mask: Image.Image,
    *,
    feather: float,
    tone_match: float = 1.0,
) -> Image.Image:
    """Paste the model's crop back into the full-resolution original.

    ``mask`` is a full-size L-mode image; white marks pixels the user wants
    changed. Only pixels inside the feathered mask are ever written.
    """
    result = base.copy()

    ctx_w, ctx_h = plan.context_size
    edited = edited_crop.convert("RGB")
    if edited.size != (ctx_w, ctx_h):
        edited = edited.resize((ctx_w, ctx_h), Image.LANCZOS)

    original_crop = base.crop(plan.context)
    region_mask = mask.crop(plan.context)
    soft = feather_mask(region_mask, feather)

    if tone_match > 0:
        # The ring is the context margin: inside the crop we sent, outside the
        # part the user actually wants changed.
        ring = ImageOps.invert(feather_mask(region_mask, max(feather, 2.0)))
        if sum(ring.histogram()[8:]) > 64:  # enough unchanged pixels to trust
            edited = match_tone(edited, original_crop, ring, strength=tone_match)

    result.paste(edited, (plan.context[0], plan.context[1]), soft)
    return result


def edit_region(
    base: Image.Image,
    selection: Box,
    render,
    *,
    mask: Image.Image | None = None,
    context_pct: float = 25.0,
    max_send_px: int = 1536,
    feather: float | None = None,
    tone_match: float = 1.0,
) -> tuple[Image.Image, CropPlan]:
    """Run the full selective-edit pipeline.

    ``render(crop, crop_mask)`` is called with the context crop (already at the
    resolution we want to send) and the matching mask, and must return the
    edited crop as a PIL image. Keeping it a callback means the provider code
    stays out of the image maths — and makes this testable without a network.
    """
    plan = plan_crop(
        selection,
        base.size,
        context_pct=context_pct,
        max_send_px=max_send_px,
    )
    full_mask = mask if mask is not None else rect_mask(base.size, plan.selection)
    if full_mask.mode != "L":
        full_mask = full_mask.convert("L")

    crop = base.crop(plan.context)
    crop_mask = full_mask.crop(plan.context)
    if crop.size != plan.send_size:
        crop = crop.resize(plan.send_size, Image.LANCZOS)
        crop_mask = crop_mask.resize(plan.send_size, Image.LANCZOS)

    edited_crop = render(crop, crop_mask)

    result = recompose(
        base,
        edited_crop,
        plan,
        full_mask,
        feather=auto_feather(plan.selection, feather),
        tone_match=tone_match,
    )
    return result, plan
