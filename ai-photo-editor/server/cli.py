"""Command-line version of the same pipeline, for scripting and batch work.

    python -m server.cli photo.jpg out.jpg \
        --box 1200,2100,1900,2600 \
        --prompt "white leather sneakers, same lighting and shadows"

Coordinates are pixels in the original image: left,top,right,bottom.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

from . import providers
from .compose import edit_region, load_image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Edit part of a photo with AI, keeping the rest untouched.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--box", help="left,top,right,bottom in original pixels. Omit to edit the whole image.")
    parser.add_argument("--mask", type=Path, help="Optional greyscale PNG; white marks what to change.")
    parser.add_argument("--provider", default="gemini", choices=sorted(providers.PROVIDERS))
    parser.add_argument("--model")
    parser.add_argument("--context-pct", type=float, default=25.0)
    parser.add_argument("--max-send-px", type=int, default=1536)
    parser.add_argument("--feather", type=float, default=None, help="Blend radius in pixels (default: automatic).")
    parser.add_argument("--tone-match", type=float, default=1.0, help="0 disables colour correction, 1 is full.")
    parser.add_argument("--quality", type=int, default=95)
    args = parser.parse_args(argv)

    base = load_image(args.input)

    def call(image: Image.Image, image_mask: Image.Image | None) -> Image.Image:
        return providers.render(
            args.provider, image, image_mask, args.prompt, model=args.model
        )

    try:
        if args.box:
            box = tuple(int(v) for v in args.box.split(","))
            if len(box) != 4:
                parser.error("--box needs exactly four numbers: left,top,right,bottom")
            mask = Image.open(args.mask).convert("L") if args.mask else None
            if mask is not None and mask.size != base.size:
                mask = mask.resize(base.size, Image.LANCZOS)
            result, plan = edit_region(
                base,
                box,
                call,
                mask=mask,
                context_pct=args.context_pct,
                max_send_px=args.max_send_px,
                feather=args.feather,
                tone_match=args.tone_match,
            )
            sent = plan.send_size[0] * plan.send_size[1] / 1e6
            total = base.width * base.height / 1e6
            print(f"Sent {sent:.2f} MP of {total:.2f} MP to {args.provider}; recomposed at {base.width}x{base.height}.")
        else:
            result = call(base, None)
            if result.size != base.size:
                result = result.resize(base.size, Image.LANCZOS)
            print(f"Regenerated the whole image at {base.width}x{base.height}.")
    except providers.ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    suffix = args.output.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        result.save(args.output, quality=args.quality, subsampling=0, optimize=True)
    elif suffix == ".webp":
        result.save(args.output, quality=args.quality, method=6)
    else:
        result.save(args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
