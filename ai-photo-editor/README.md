# Selective AI Photo Editor

Edit **one part** of a photo with a generative model — the shoes, the sky, a
sign in the background — and get the whole photo back at its original
resolution, with everything else untouched.

The usual problem with AI photo editing is that "change the shoes" means the
model regenerates the entire frame, and faces, hands and text come back subtly
wrong. This tool never sends the whole photo unless you ask it to. It crops your
selection, edits only that crop, and pastes the result back into the original
pixels through a feathered mask.

## How it works

```
original photo (e.g. 6000 x 4000)
        │
        ├─ selection ──────────────► expand by a context margin
        │  (rectangle or brush)      so the model sees surroundings
        │                                     │
        │                            crop, resample to ~1536px
        │                                     │
        │                            send ONLY this to the model
        │                                     │
        │                            resize the answer back to the
        │                            crop's exact pixel size
        │                                     │
        │                            correct brightness/tint drift using
        │                            the untouched context ring
        │                                     ▼
        └──────────────────────────► paste through a feathered mask
                                              │
                                     original resolution out,
                                     every other pixel identical
```

Five details do the real work:

- **Context margin.** The crop sent to the model is bigger than your selection
  (25% by default), so it can match perspective, lighting and shadow direction.
  The margin is *seen* by the model but never pasted back — only your selection
  is.
- **Resolution.** Small selections are upscaled before sending so the model has
  enough pixels to work with, then resampled back down with Lanczos. Big ones
  are capped so you stay inside the model's input limits.
- **Feathered edges.** The mask is Gaussian-blurred (auto: ~1.5% of the
  selection's short side) so there's no visible seam.
- **Colour matching.** Models often return a crop a shade brighter or cooler.
  The tool measures that drift on the context ring — which should be unchanged —
  and cancels it. The correction is bounded, so asking for red shoes in a blue
  scene still gives you red shoes.
- **Full-resolution recomposition.** The original never gets downscaled. The
  browser only handles a preview; the pixels live server-side at full size.

## Running it

```bash
cd ai-photo-editor
./run.sh                      # creates a venv, installs deps, serves on :8000
```

Then open http://127.0.0.1:8000.

You need an API key for whichever image model you want to use. Either export it
before starting, or paste it into the settings panel (it's kept in your
browser's localStorage, not on the server):

| Provider | Env var | Notes |
|---|---|---|
| Google Gemini ("Nano Banana") | `GEMINI_API_KEY` | Default. Best at "keep everything, change this one thing". |
| OpenAI Images | `OPENAI_API_KEY` | `gpt-image-1`, supports a real inpainting mask. |
| Stability AI | `STABILITY_API_KEY` | Dedicated inpaint endpoint. |

```bash
export GEMINI_API_KEY=...   # then ./run.sh
```

## Using it

1. Drop a photo in.
2. Pick **Selected area** (the point of this tool) or **Whole image** (the
   normal, riskier thing).
3. Select with the **Rectangle** tool, or paint an exact shape with the
   **Brush** — a painted mask means only the shape you painted changes, not its
   bounding box.
4. Describe the change. Describe the *whole selected area*, not just the
   difference: the model sees a crop, not your previous sentence. "White leather
   sneakers with white laces, same lighting and shadows" beats "make them white".
5. Generate. The activity log tells you exactly how much of the frame was sent
   ("sent 0.61 MP of 6 MP").
6. Undo, try again, or download at full resolution.

**Download format matters.** "PNG (lossless)" gives you a file where the
unedited pixels are byte-for-byte the original. Re-encoding to JPEG will
re-compress the entire image — visually identical, but not bit-identical.

## Command line

Same pipeline, for scripting and batch work:

```bash
python -m server.cli photo.jpg out.png \
    --box 1200,2100,1900,2600 \
    --prompt "white leather sneakers, same lighting and shadows"

# whole-image edit: just leave off --box
# custom shape: --mask mask.png   (white marks what to change)
```

## Tuning

All in the settings panel, all with sensible defaults:

| Setting | What it does | When to change it |
|---|---|---|
| Context margin | How much surrounding is sent with the crop | Raise it when the edit needs to match scene lighting; lower it when the surroundings are distracting the model |
| Detail sent | Resolution of the crop sent to the model | Raise for fine texture, lower to cut cost/latency |
| Edge feather | Blend radius at the mask edge | Raise for soft backgrounds, lower for hard edges like a phone screen |
| Colour match | Strength of the drift correction | Drop to 0 if you *want* a dramatic lighting change in the region |

## Tests

No network and no API key needed — the model is stubbed and the image maths is
checked directly.

```bash
python -m pytest tests -q
```

The tests that matter assert the core promise: after a region edit, pixels
outside the selection are byte-for-byte identical, the output is the original
size, a painted mask confines the change to the painted shape, and the tone
correction cannot cancel an intentional colour change.

## Layout

```
server/compose.py    crop planning, feathering, tone matching, recomposition
server/providers.py  Gemini / OpenAI / Stability backends behind one interface
server/app.py        HTTP API; holds full-resolution versions per session
server/cli.py        command-line front end for the same pipeline
web/                 single-page UI (no build step, no dependencies)
tests/               offline tests for the pipeline and the API
```

Images live in server memory for the session only (6h TTL, 24 images max) and
are never written to disk. Nothing is sent anywhere except the crop you're
editing, to the provider you chose.
