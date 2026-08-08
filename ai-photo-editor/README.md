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

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | unset | Requires a password on every route but `/healthz`. Set it whenever the app is reachable by anyone but you. |
| `MAX_INPUT_MP` | 80 | Largest upload accepted, in megapixels. |
| `MAX_VERSIONS` | 8 | Undo depth. The original is never dropped. |
| `MEMORY_BUDGET_MB` | 1500 | Total held across sessions before evicting the least recently used. |
| `PORT` | 8000 | Port to serve on. |

| Provider | Env var | Notes |
|---|---|---|
| Google Gemini ("Nano Banana") | `GEMINI_API_KEY` | Default. Best at "keep everything, change this one thing". |
| OpenAI Images | `OPENAI_API_KEY` | `gpt-image-1`, supports a real inpainting mask. |
| Stability AI | `STABILITY_API_KEY` | Dedicated inpaint endpoint. |

```bash
export GEMINI_API_KEY=...   # then ./run.sh
```

## On a phone

There's no hosted version — it runs on your own machine. To use it from a
phone, serve it on your local network and open it from the same Wi-Fi:

```bash
./run.sh --lan
```

That prints the address to type into the phone (something like
`http://192.168.1.42:8000`). Both devices need to be on the same network, and
a VPN on either one will usually break it.

The UI is built for touch: **pinch with two fingers to zoom, one finger draws**.
Zoom in before selecting — a fingertip covers a lot of a photo shown 390px
wide, and zooming is what makes a tight selection around something like a shoe
possible. "Fit" returns to the whole frame.

Photos from an iPhone's library are handed over as JPEG by Safari, so HEIC
originals work without converting anything first.

Two caveats: `--lan` means anyone else on that network can open the page and
spend your API key, so set `APP_PASSWORD` (below) before you do it. And iOS
Safari will drop the tab from memory if you switch away for a long time — the
server keeps your image for 6 hours, so reloading gets you back to where you
were.

## Deploying a private preview

For testing from a phone when you're not at home, deploy it. `render.yaml` at
the repo root is a Render Blueprint that does this in one pass — from Safari,
no CLI needed:

1. https://render.com → **New** → **Blueprint** → connect this repo.
2. It reads `render.yaml`, and asks for the one secret marked `sync: false`:
   paste your `GEMINI_API_KEY`.
3. Deploy. You get `https://ai-photo-editor-xxxx.onrender.com`.
4. In the service's **Environment** tab, copy the generated `APP_PASSWORD`.
   Opening the URL prompts for a login — any username, that password.
5. In Safari: Share → **Add to Home Screen**. It opens full-screen like an app.

Every push to the branch in `render.yaml` redeploys automatically, so a change
made from your phone is testable on your phone a few minutes later.

**Always set `APP_PASSWORD`.** Any deployed URL is reachable by anyone who
finds it, and an open page means an open API key. The blueprint generates one
for you; `APP_PASSWORD` is enforced on every route except `/healthz`.

**Free tier notes.** The instance sleeps after 15 minutes idle, so the first
request takes ~50s to wake. It has 512 MB, and this app holds decoded images in
memory — hence the caps in `render.yaml`:

| Variable | Free-tier value | What it does |
|---|---|---|
| `MAX_INPUT_MP` | 13 | Refuses larger uploads instead of dying. iPhone photos are 12 MP; a 48 MP ProRAW shot is refused. |
| `MAX_VERSIONS` | 3 | Undo history depth. The original is always kept; the oldest *edit* is dropped. |
| `MEMORY_BUDGET_MB` | 120 | Total across sessions before the least-recently-used are evicted. |

Measured on a 12 MP photo with those settings, memory peaks at ~340 MB and
plateaus. At 20 MP it peaked at ~478 MB, which is too close to the limit. On a
paid instance, raise all three.

Nothing is written to disk, so there's no volume to configure and a redeploy
loses in-flight images — which is what you want for photos of yourself sitting
on someone else's server.

### Why not Vercel

Considered and rejected, for a reason that would apply to any image tool:
Vercel functions cap request *and response* bodies at 4.5 MB, enforced at the
infrastructure level. A 12 MP result is 15–30 MB as PNG or 5–9 MB as JPEG, so
the download endpoint — the entire deliverable — fails with
`FUNCTION_PAYLOAD_TOO_LARGE`. Functions are also stateless, and this app keeps
the full-resolution image in memory across upload → edit → undo → download.

Both are solvable: the client uploads and downloads straight to Vercel Blob
with signed URLs so the bytes never pass through a function. But that is an
architecture change, it puts your photos in cloud storage rather than in RAM,
and using the AI SDK for the model call means porting this whole pipeline from
Pillow to sharp. Revisit only if the tool earns it.

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
