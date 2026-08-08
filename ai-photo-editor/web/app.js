/* Selective AI photo editor — browser side.
 *
 * The browser only ever sees a downscaled preview. Selections are drawn in
 * preview pixels and scaled up to the original's coordinates before being sent,
 * so the crop the server takes is at full resolution no matter how big the
 * photo is or how small the screen is.
 */

const $ = (id) => document.getElementById(id);

const el = {
  dropzone: $("dropzone"),
  file: $("file"),
  wrap: $("canvasWrap"),
  photo: $("photo"),
  overlay: $("overlay"),
  busy: $("busy"),
  busyText: $("busyText"),
  meta: $("meta"),
  modeToggle: $("modeToggle"),
  modeHint: $("modeHint"),
  toolBlock: $("toolBlock"),
  toolToggle: $("toolToggle"),
  brushSize: $("brushSize"),
  brushSizeOut: $("brushSizeOut"),
  brushSizeRow: $("brushSizeRow"),
  clearSel: $("clearSel"),
  prompt: $("prompt"),
  generate: $("generate"),
  provider: $("provider"),
  model: $("model"),
  apiKey: $("apiKey"),
  keyHint: $("keyHint"),
  contextPct: $("contextPct"),
  ctxOut: $("ctxOut"),
  maxSend: $("maxSend"),
  sendOut: $("sendOut"),
  feather: $("feather"),
  featherOut: $("featherOut"),
  tone: $("tone"),
  toneOut: $("toneOut"),
  undo: $("undo"),
  original: $("original"),
  download: $("download"),
  dlFormat: $("dlFormat"),
  log: $("log"),
};

const state = {
  session: null, // server response for the current image
  mode: "region",
  tool: "rect",
  rect: null, // {x0,y0,x1,y1} in preview pixels
  maskCanvas: null, // painted brush mask, preview sized
  hasPaint: false,
  drawing: false,
  providers: [],
  busy: false,
};

const ctx = () => el.overlay.getContext("2d");

/* ------------------------------------------------------------------ log */

function log(message, kind = "") {
  const li = document.createElement("li");
  li.className = kind;
  const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  li.textContent = `${time}  ${message}`;
  el.log.prepend(li);
  while (el.log.children.length > 40) el.log.lastChild.remove();
}

/* ------------------------------------------------------- providers/keys */

const keyStorageId = (provider) => `spe.key.${provider}`;

async function loadProviders() {
  const res = await fetch("/api/providers");
  const body = await res.json();
  state.providers = body.providers;
  el.provider.innerHTML = "";
  for (const p of state.providers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.key_in_env ? `${p.label} — key found` : p.label;
    el.provider.append(opt);
  }
  const withKey = state.providers.find((p) => p.key_in_env || localStorage.getItem(keyStorageId(p.id)));
  el.provider.value = (withKey || state.providers[0]).id;
  syncProvider();
}

function currentProvider() {
  return state.providers.find((p) => p.id === el.provider.value);
}

function syncProvider() {
  const p = currentProvider();
  if (!p) return;
  el.model.innerHTML = "";
  for (const m of p.models) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    el.model.append(opt);
  }
  el.model.value = p.default_model;
  el.apiKey.value = localStorage.getItem(keyStorageId(p.id)) || "";
  el.keyHint.innerHTML = p.key_in_env
    ? `Using <code>${p.env_var}</code> from the server environment. A key typed here overrides it.`
    : `Needs a key. Set <code>${p.env_var}</code> before starting the server, or paste one here (stored in this browser only). <a class="link" href="${p.docs}" target="_blank" rel="noreferrer">Docs</a>`;
}

el.provider.addEventListener("change", syncProvider);
el.apiKey.addEventListener("change", () => {
  const p = currentProvider();
  if (!p) return;
  if (el.apiKey.value.trim()) localStorage.setItem(keyStorageId(p.id), el.apiKey.value.trim());
  else localStorage.removeItem(keyStorageId(p.id));
});

/* ------------------------------------------------------------- uploading */

["dragenter", "dragover"].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    el.dropzone.classList.add("hover");
  })
);
["dragleave", "drop"].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => {
    e.preventDefault();
    el.dropzone.classList.remove("hover");
  })
);
el.dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files?.[0];
  if (file) upload(file);
});
el.file.addEventListener("change", () => {
  if (el.file.files?.[0]) upload(el.file.files[0]);
});

async function upload(file) {
  setBusy(true, "Uploading…");
  try {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/images", { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    state.session = await res.json();
    await showCurrent();
    el.dropzone.hidden = true;
    el.wrap.hidden = false;
    el.meta.hidden = false;
    clearSelection();
    log(`Loaded ${state.session.name} — ${state.session.width}x${state.session.height}`, "ok");
  } catch (err) {
    log(`Upload failed: ${err.message}`, "err");
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------- preview */

function showCurrent() {
  return new Promise((resolve) => {
    const url = `${state.session.preview_url}&t=${Date.now()}`;
    el.photo.onload = () => {
      sizeOverlay();
      refreshButtons();
      updateMeta();
      resolve();
    };
    el.photo.src = url;
  });
}

function sizeOverlay() {
  const w = el.photo.naturalWidth;
  const h = el.photo.naturalHeight;
  if (el.overlay.width !== w || el.overlay.height !== h) {
    el.overlay.width = w;
    el.overlay.height = h;
    const mask = document.createElement("canvas");
    mask.width = w;
    mask.height = h;
    state.maskCanvas = mask;
    state.hasPaint = false;
    state.rect = null;
  }
  draw();
}

/** Preview pixels -> original image pixels. */
function previewScale() {
  return state.session.width / el.overlay.width;
}

function updateMeta() {
  const s = state.session;
  const total = ((s.width * s.height) / 1e6).toFixed(1);
  let text = `${s.width} x ${s.height} (${total} MP) · version ${s.version} of ${s.versions - 1} edit(s)`;
  const sel = selectionInOriginal();
  if (sel && state.mode === "region") {
    const pct = (((sel.right - sel.left) * (sel.bottom - sel.top)) / (s.width * s.height)) * 100;
    text += ` · selection ${Math.round(sel.right - sel.left)} x ${Math.round(sel.bottom - sel.top)} (${pct.toFixed(1)}% of frame)`;
  }
  el.meta.textContent = text;
}

/* ------------------------------------------------------------ selection */

function draw() {
  const c = ctx();
  const { width: w, height: h } = el.overlay;
  c.clearRect(0, 0, w, h);
  if (state.mode === "whole") return;

  if (state.hasPaint) {
    // Tint the painted mask so you can see exactly what will be regenerated.
    c.save();
    c.globalAlpha = 0.55;
    c.drawImage(state.maskCanvas, 0, 0);
    c.globalCompositeOperation = "source-atop";
    c.fillStyle = "#c6f04a";
    c.fillRect(0, 0, w, h);
    c.restore();
  }

  const r = normalisedRect();
  if (r && !state.hasPaint) {
    c.save();
    c.fillStyle = "rgba(10,12,8,0.45)";
    c.beginPath();
    c.rect(0, 0, w, h);
    c.rect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
    c.fill("evenodd");
    c.restore();

    c.strokeStyle = "#c6f04a";
    c.lineWidth = Math.max(2, w / 500);
    c.setLineDash([10, 7]);
    c.strokeRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
    c.setLineDash([]);
  }
}

function normalisedRect() {
  if (!state.rect) return null;
  const { x0, y0, x1, y1 } = state.rect;
  const r = {
    x0: Math.min(x0, x1),
    y0: Math.min(y0, y1),
    x1: Math.max(x0, x1),
    y1: Math.max(y0, y1),
  };
  return r.x1 - r.x0 > 3 && r.y1 - r.y0 > 3 ? r : null;
}

/** Bounding box of painted pixels, in preview coordinates. */
function paintedBounds() {
  const c = state.maskCanvas.getContext("2d");
  const { width: w, height: h } = state.maskCanvas;
  const data = c.getImageData(0, 0, w, h).data;
  let minX = w, minY = h, maxX = -1, maxY = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (data[(y * w + x) * 4 + 3] > 12) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  return maxX < 0 ? null : { x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1 };
}

function selectionInOriginal() {
  const r = state.hasPaint ? paintedBounds() : normalisedRect();
  if (!r) return null;
  const s = previewScale();
  return {
    left: Math.round(r.x0 * s),
    top: Math.round(r.y0 * s),
    right: Math.round(r.x1 * s),
    bottom: Math.round(r.y1 * s),
  };
}

function eventPoint(e) {
  const rect = el.overlay.getBoundingClientRect();
  return {
    x: ((e.clientX - rect.left) / rect.width) * el.overlay.width,
    y: ((e.clientY - rect.top) / rect.height) * el.overlay.height,
  };
}

function paintAt(p, erase) {
  const c = state.maskCanvas.getContext("2d");
  const radius = (Number(el.brushSize.value) / 2) * (el.overlay.width / el.overlay.getBoundingClientRect().width);
  c.globalCompositeOperation = erase ? "destination-out" : "source-over";
  c.fillStyle = "#ffffff";
  c.beginPath();
  c.arc(p.x, p.y, radius, 0, Math.PI * 2);
  c.fill();
  c.globalCompositeOperation = "source-over";
  if (!erase) state.hasPaint = true;
}

el.overlay.addEventListener("pointerdown", (e) => {
  if (!state.session || state.mode === "whole" || state.busy) return;
  el.overlay.setPointerCapture(e.pointerId);
  state.drawing = true;
  const p = eventPoint(e);
  if (state.tool === "rect") {
    state.rect = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    clearPaint();
  } else {
    state.rect = null;
    paintAt(p, state.tool === "erase");
  }
  draw();
});

el.overlay.addEventListener("pointermove", (e) => {
  if (!state.drawing) return;
  const p = eventPoint(e);
  if (state.tool === "rect") {
    state.rect.x1 = p.x;
    state.rect.y1 = p.y;
  } else {
    paintAt(p, state.tool === "erase");
  }
  draw();
});

["pointerup", "pointercancel"].forEach((ev) =>
  el.overlay.addEventListener(ev, () => {
    if (!state.drawing) return;
    state.drawing = false;
    if (state.tool === "erase" && !paintedBounds()) state.hasPaint = false;
    draw();
    updateMeta();
    refreshButtons();
  })
);

function clearPaint() {
  if (!state.maskCanvas) return;
  const c = state.maskCanvas.getContext("2d");
  c.clearRect(0, 0, state.maskCanvas.width, state.maskCanvas.height);
  state.hasPaint = false;
}

function clearSelection() {
  state.rect = null;
  clearPaint();
  draw();
  updateMeta();
  refreshButtons();
}

el.clearSel.addEventListener("click", clearSelection);

/* --------------------------------------------------------------- modes */

el.modeToggle.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  state.mode = button.dataset.mode;
  [...el.modeToggle.children].forEach((b) => b.classList.toggle("active", b === button));
  el.toolBlock.hidden = state.mode === "whole";
  el.modeHint.textContent =
    state.mode === "whole"
      ? "The entire photo is regenerated. Faster to describe, but the model can redraw faces and details you wanted kept."
      : "Only the selected area is sent to the model and pasted back. Faces and detail elsewhere can't change.";
  draw();
  updateMeta();
  refreshButtons();
});

el.toolToggle.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  state.tool = button.dataset.tool;
  [...el.toolToggle.children].forEach((b) => b.classList.toggle("active", b === button));
  el.brushSizeRow.hidden = state.tool === "rect";
});

/* ------------------------------------------------------------ settings */

const bindOut = (input, out, format) => {
  const sync = () => (out.textContent = format(input.value));
  input.addEventListener("input", sync);
  sync();
};
bindOut(el.contextPct, el.ctxOut, (v) => v);
bindOut(el.maxSend, el.sendOut, (v) => v);
bindOut(el.feather, el.featherOut, (v) => (Number(v) < 0 ? "auto" : `${v}px`));
bindOut(el.tone, el.toneOut, (v) => v);
bindOut(el.brushSize, el.brushSizeOut, (v) => v);

/* ------------------------------------------------------------ generate */

function refreshButtons() {
  const ready = Boolean(state.session) && !state.busy;
  const hasSelection = state.mode === "whole" || Boolean(selectionInOriginal());
  el.generate.disabled = !ready || !hasSelection;
  el.download.disabled = !ready;
  el.undo.disabled = !ready || state.session.version < 1;
  el.original.disabled = !ready || state.session.version < 1;
}

function setBusy(on, text = "Working…") {
  state.busy = on;
  el.busy.hidden = !on;
  el.busyText.textContent = text;
  refreshButtons();
}

function maskBlob() {
  // Flatten the painted alpha onto black so the server gets a plain
  // white-means-edit greyscale mask.
  const flat = document.createElement("canvas");
  flat.width = state.maskCanvas.width;
  flat.height = state.maskCanvas.height;
  const c = flat.getContext("2d");
  c.fillStyle = "#000";
  c.fillRect(0, 0, flat.width, flat.height);
  c.drawImage(state.maskCanvas, 0, 0);
  return new Promise((resolve) => flat.toBlob(resolve, "image/png"));
}

el.generate.addEventListener("click", async () => {
  if (!state.session) return;
  const prompt = el.prompt.value.trim();
  if (!prompt) {
    log("Describe the change you want first.", "err");
    return;
  }

  const form = new FormData();
  form.append("prompt", prompt);
  form.append("mode", state.mode);
  form.append("provider", el.provider.value);
  form.append("model", el.model.value);
  form.append("context_pct", el.contextPct.value);
  form.append("max_send_px", el.maxSend.value);
  form.append("tone_match", String(Number(el.tone.value) / 100));
  if (Number(el.feather.value) >= 0) form.append("feather", el.feather.value);
  if (el.apiKey.value.trim()) form.append("api_key", el.apiKey.value.trim());

  if (state.mode === "region") {
    const sel = selectionInOriginal();
    if (!sel) {
      log("Select an area first.", "err");
      return;
    }
    form.append("selection", JSON.stringify(sel));
    if (state.hasPaint) form.append("mask", await maskBlob(), "mask.png");
  }

  setBusy(true, state.mode === "whole" ? "Regenerating the whole image…" : "Editing the selected area…");
  try {
    const res = await fetch(`/api/images/${state.session.id}/edit`, { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    state.session = body;
    await showCurrent();
    const info = body.edit;
    if (info.mode === "region") {
      log(
        `Edited region in ${info.seconds}s — sent ${info.megapixels_sent} MP of ${info.megapixels_total} MP, recomposed at full size.`,
        "ok"
      );
    } else {
      log(`Regenerated whole image in ${info.seconds}s.`, "ok");
    }
  } catch (err) {
    log(err.message, "err");
  } finally {
    setBusy(false);
  }
});

/* -------------------------------------------------------- undo/download */

el.undo.addEventListener("click", async () => {
  setBusy(true, "Undoing…");
  try {
    const res = await fetch(`/api/images/${state.session.id}/undo`, { method: "POST" });
    state.session = await res.json();
    await showCurrent();
    log("Reverted the last edit.");
  } finally {
    setBusy(false);
  }
});

el.original.addEventListener("click", async () => {
  setBusy(true, "Restoring original…");
  try {
    const res = await fetch(`/api/images/${state.session.id}/revert`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: 0 }),
    });
    state.session = await res.json();
    await showCurrent();
    log("Back to the original upload.");
  } finally {
    setBusy(false);
  }
});

el.download.addEventListener("click", () => {
  const url = `/api/images/${state.session.id}/download?format=${el.dlFormat.value}&v=${state.session.version}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  a.click();
});

window.addEventListener("resize", draw);

loadProviders().catch((err) => log(`Couldn't load providers: ${err.message}`, "err"));
