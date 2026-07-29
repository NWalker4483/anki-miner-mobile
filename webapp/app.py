"""OCR web app — upload a picture, extract text with Tesseract.

Future: wire in translation + Anki card creation. For now this proves the
web UI works and that the app can reach the Anki container's AnkiConnect API
over the shared Docker network.

Design notes:
- Single code path per operation, failures are loud (surface the real error,
  never swallow it into a generic 200).
- AnkiConnect is called server-side via httpx, so no browser CORS config is
  needed. Inside the compose network the Anki service is reachable by name on
  its *container* port 8765 (the host 8766 remap does not apply here).
"""
import io
import os

import httpx
import pytesseract
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image

ANKICONNECT_URL = os.environ.get("ANKICONNECT_URL", "http://anki:8765")
TTS_URL = os.environ.get("TTS_URL", "http://tts:8080")
# Default to mixed Chinese + English since the target decks are Mandarin.
DEFAULT_OCR_LANG = os.environ.get("OCR_LANG", "chi_sim+eng")

# Gemini (Google Generative Language API). Key is read from the environment,
# which docker-compose populates from the gitignored .env file.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

app = FastAPI(title="Anki OCR")


def anki_request(action: str, **params):
    """Call AnkiConnect. Raises loudly on transport or API error."""
    payload = {"action": action, "version": 6, "params": params}
    try:
        resp = httpx.post(ANKICONNECT_URL, json=payload, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach AnkiConnect at {ANKICONNECT_URL}: {exc}",
        )
    body = resp.json()
    if body.get("error") is not None:
        raise HTTPException(status_code=502, detail=f"AnkiConnect error: {body['error']}")
    return body["result"]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/anki/status")
def anki_status():
    """Prove the app can reach the Anki container. Returns decks or the error."""
    try:
        version = anki_request("version")
        decks = anki_request("deckNames")
        return {"connected": True, "version": version, "decks": decks, "url": ANKICONNECT_URL}
    except HTTPException as exc:
        return JSONResponse(
            status_code=200,
            content={"connected": False, "error": exc.detail, "url": ANKICONNECT_URL},
        )


@app.post("/api/ocr")
async def ocr(file: UploadFile = File(...), lang: str = Form(DEFAULT_OCR_LANG)):
    """Run Tesseract OCR on an uploaded image and return the extracted text."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload.")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Not a readable image: {exc}")
    try:
        text = pytesseract.image_to_string(image, lang=lang)
    except pytesseract.TesseractError as exc:
        raise HTTPException(status_code=500, detail=f"OCR failed (lang={lang!r}): {exc}")
    return {"lang": lang, "filename": file.filename, "text": text}


@app.post("/api/tts")
def tts(text: str = Form(...), speed: float = Form(1.0)):
    """Proxy text to the Piper TTS service and stream the WAV back."""
    try:
        resp = httpx.post(f"{TTS_URL}/tts", json={"text": text, "speed": speed}, timeout=60.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"TTS service error: {exc.response.text}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach TTS at {TTS_URL}: {exc}")
    return Response(content=resp.content, media_type="audio/wav")


@app.post("/api/chat")
def chat(text: str = Form(...)):
    """Send text to Gemini and return the model's reply."""
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured.")
    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
    try:
        resp = httpx.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": text}]}]},
            timeout=60.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Gemini error: {exc.response.text}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Gemini: {exc}")
    data = resp.json()
    try:
        reply = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail=f"Unexpected Gemini response: {data}")
    return {"model": GEMINI_MODEL, "reply": reply}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anki OCR</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto;
         padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.4rem; }
  .status { padding: .6rem .9rem; border-radius: 8px; font-size: .9rem; margin: 1rem 0; }
  .ok { background: #10391033; border: 1px solid #2e7d32; }
  .bad { background: #3d101033; border: 1px solid #c62828; }
  form { display: grid; gap: .8rem; margin: 1.5rem 0; }
  label { font-weight: 600; font-size: .9rem; }
  input, button { font: inherit; padding: .5rem; }
  button { cursor: pointer; border-radius: 6px; border: 1px solid #888; }
  #preview { max-width: 100%; border-radius: 8px; margin-top: .5rem; display: none; }
  pre { white-space: pre-wrap; word-break: break-word; background: #8881;
        padding: 1rem; border-radius: 8px; min-height: 3rem; }
  .muted { color: #888; font-size: .85rem; }
</style>
</head>
<body>
<h1>Anki OCR</h1>
<p class="muted">Upload a picture, extract text. Translation &amp; card creation coming later.</p>

<div id="anki-status" class="status">Checking AnkiConnect…</div>

<form id="ocr-form">
  <div>
    <label for="file">Image</label><br>
    <input type="file" id="file" name="file" accept="image/*" required>
  </div>
  <img id="preview" alt="preview">
  <div>
    <label for="lang">OCR language</label><br>
    <input type="text" id="lang" name="lang" value="chi_sim+eng">
    <span class="muted">e.g. <code>eng</code>, <code>chi_sim</code>, <code>chi_tra</code>, or joined with <code>+</code></span>
  </div>
  <button type="submit">Run OCR</button>
</form>

<h2 style="font-size:1.1rem">Extracted text</h2>
<pre id="result">—</pre>

<hr style="margin:2rem 0; border:none; border-top:1px solid #8884;">

<h2 style="font-size:1.1rem">Text to speech</h2>
<form id="tts-form">
  <div>
    <label for="tts-text">Text</label><br>
    <textarea id="tts-text" name="text" rows="3" style="width:100%; font:inherit; padding:.5rem;"
      >你好，欢迎使用文字转语音。</textarea>
  </div>
  <div style="margin:.6rem 0;">
    <label for="tts-speed">Speed</label>
    <input type="range" id="tts-speed" min="0.5" max="1.5" step="0.1" value="1.0">
    <span id="tts-speed-val" class="muted">1.0×</span>
  </div>
  <button type="submit">Speak</button>
</form>
<audio id="tts-audio" controls style="width:100%; margin-top:1rem; display:none;"></audio>
<div id="tts-msg" class="muted" style="margin-top:.5rem;"></div>

<hr style="margin:2rem 0; border:none; border-top:1px solid #8884;">

<h2 style="font-size:1.1rem">Ask Gemini</h2>
<form id="chat-form">
  <div>
    <label for="chat-text">Prompt</label><br>
    <textarea id="chat-text" name="text" rows="3" style="width:100%; font:inherit; padding:.5rem;"
      >用一句话解释什么是间隔重复。</textarea>
  </div>
  <button type="submit" style="margin-top:.6rem;">Send</button>
</form>
<h3 style="font-size:.95rem; margin-bottom:.3rem;">Response</h3>
<pre id="chat-result">—</pre>
<div id="chat-msg" class="muted"></div>

<script>
async function loadAnkiStatus() {
  const el = document.getElementById('anki-status');
  try {
    const r = await fetch('/api/anki/status');
    const d = await r.json();
    if (d.connected) {
      el.className = 'status ok';
      el.textContent = `AnkiConnect v${d.version} reachable at ${d.url} — ${d.decks.length} deck(s): ${d.decks.join(', ')}`;
    } else {
      el.className = 'status bad';
      el.textContent = `AnkiConnect unreachable: ${d.error}`;
    }
  } catch (e) {
    el.className = 'status bad';
    el.textContent = 'Failed to query AnkiConnect status: ' + e;
  }
}

const fileInput = document.getElementById('file');
const preview = document.getElementById('preview');
fileInput.addEventListener('change', () => {
  const f = fileInput.files[0];
  if (f) { preview.src = URL.createObjectURL(f); preview.style.display = 'block'; }
});

document.getElementById('ocr-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = document.getElementById('result');
  out.textContent = 'Running OCR…';
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('lang', document.getElementById('lang').value);
  try {
    const r = await fetch('/api/ocr', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) { out.textContent = 'Error: ' + (d.detail || r.status); return; }
    out.textContent = d.text.trim() || '(no text detected)';
  } catch (err) {
    out.textContent = 'Request failed: ' + err;
  }
});

// --- Text to speech ---
const speed = document.getElementById('tts-speed');
const speedVal = document.getElementById('tts-speed-val');
speed.addEventListener('input', () => { speedVal.textContent = (+speed.value).toFixed(1) + '×'; });

document.getElementById('tts-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = document.getElementById('tts-msg');
  const audio = document.getElementById('tts-audio');
  const text = document.getElementById('tts-text').value.trim();
  if (!text) { msg.textContent = 'Enter some text first.'; return; }
  msg.textContent = 'Synthesizing…';
  const fd = new FormData();
  fd.append('text', text);
  fd.append('speed', speed.value);
  try {
    const r = await fetch('/api/tts', { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = r.status;
      try { detail = (await r.json()).detail; } catch (_) {}
      msg.textContent = 'Error: ' + detail;
      return;
    }
    const blob = await r.blob();
    audio.src = URL.createObjectURL(blob);
    audio.style.display = 'block';
    audio.play();
    msg.textContent = 'Done (' + Math.round(blob.size / 1024) + ' KB).';
  } catch (err) {
    msg.textContent = 'Request failed: ' + err;
  }
});

// --- Ask Gemini ---
document.getElementById('chat-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const out = document.getElementById('chat-result');
  const msg = document.getElementById('chat-msg');
  const text = document.getElementById('chat-text').value.trim();
  if (!text) { msg.textContent = 'Enter a prompt first.'; return; }
  out.textContent = 'Thinking…'; msg.textContent = '';
  const fd = new FormData();
  fd.append('text', text);
  try {
    const r = await fetch('/api/chat', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) { out.textContent = 'Error: ' + (d.detail || r.status); return; }
    out.textContent = d.reply;
    msg.textContent = 'model: ' + d.model;
  } catch (err) {
    out.textContent = 'Request failed: ' + err;
  }
});

loadAnkiStatus();
</script>
</body>
</html>"""
