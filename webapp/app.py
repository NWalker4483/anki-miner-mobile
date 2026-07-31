"""Screenshot → Anki web app.

Pipeline (all LLM I/O is JSON validated by Pydantic models — see pipeline.py):
  1. /api/analyze   image -> Gemini -> {description, terms[]}
  2. deck search    AnkiConnect: split terms into already-in-deck vs new,
                    plus a sample of known vocab (done inside /api/generate)
  3. /api/generate  new terms + known vocab -> Gemini -> {cards[]}
  4. /api/add-cards selected cards -> AnkiConnect addNotes + sync

Also keeps standalone OCR (Tesseract), TTS (Piper proxy), and a plain Gemini
chat endpoint. AnkiConnect is called server-side; inside the compose network the
Anki service is reachable by name on its *container* port 8765.
"""
import base64
import hashlib
import io
import json
import os
import re

import httpx
import pytesseract
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image

import cedict
import pipeline

ANKICONNECT_URL = os.environ.get("ANKICONNECT_URL", "http://anki:8765")
TTS_URL = os.environ.get("TTS_URL", "http://tts:8080")
DEFAULT_OCR_LANG = os.environ.get("OCR_LANG", "chi_sim+eng")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# How many existing deck cards to sample as "vocabulary the learner knows",
# and the max target words per generation request.
KNOWN_SAMPLE = 30
MAX_TARGETS = 12
CJK = re.compile(r"[一-鿿]")
# Note type new cards are added as. The rich HSK model (big hanzi, pinyin,
# meaning, example sentence + audio) is what the user's HSK1 deck uses.
DEFAULT_CARD_MODEL = os.environ.get("CARD_MODEL", "HSK")

app = FastAPI(title="Screenshot → Anki")


def anki_request(action: str, **params):
    """Call AnkiConnect. Raises loudly on transport or API error."""
    payload = {"action": action, "version": 6, "params": params}
    try:
        resp = httpx.post(ANKICONNECT_URL, json=payload, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach AnkiConnect at {ANKICONNECT_URL}: {exc}")
    body = resp.json()
    if body.get("error") is not None:
        raise HTTPException(status_code=502, detail=f"AnkiConnect error: {body['error']}")
    return body["result"]


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _note_front(fields: dict) -> str:
    """First field of a note (by order) with HTML stripped — the 'front'/hanzi."""
    if not fields:
        return ""
    first = min(fields.items(), key=lambda kv: kv[1].get("order", 0))
    return _strip_html(first[1].get("value", ""))


def deck_known_words(deck: str, limit: int = KNOWN_SAMPLE) -> list[str]:
    ids = anki_request("findNotes", query=f'deck:"{deck}"')
    if not ids:
        return []
    infos = anki_request("notesInfo", notes=ids[:limit])
    words = [_note_front(n.get("fields", {})) for n in infos]
    return [w for w in words if w]


def words_already_present(deck: str, words: list[str], model: str) -> set[str]:
    """Which of `words` Anki would reject as duplicates — using the SAME check
    addNotes uses (note-type first field, collection-wide), so generation never
    produces a card that can't be added. Note Anki dedup is NOT deck-scoped: a word
    already in another deck of the same note type still counts as a duplicate.
    """
    if not words:
        return set()
    probe = [
        {"deckName": deck, "modelName": model, "fields": {"Key": w},
         "options": {"allowDuplicate": False}, "tags": []}
        for w in words
    ]
    can_add = anki_request("canAddNotes", notes=probe)
    return {w for w, ok in zip(words, can_add) if not ok}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/define")
def define(word: str):
    """CC-CEDICT lookup for a headword — all readings and senses (for grounding/debug)."""
    entries = cedict.lookup(word)
    return {
        "word": word,
        "loaded": cedict.loaded_count(),
        "entries": [
            {"traditional": e.traditional, "simplified": e.simplified,
             "pinyin": e.pinyin, "senses": e.senses}
            for e in entries
        ],
    }


@app.get("/api/anki/status")
def anki_status():
    try:
        version = anki_request("version")
        decks = anki_request("deckNames")
        return {"connected": True, "version": version, "decks": decks, "url": ANKICONNECT_URL}
    except HTTPException as exc:
        return JSONResponse(status_code=200, content={"connected": False, "error": exc.detail, "url": ANKICONNECT_URL})


# --- Screenshot → Anki pipeline -------------------------------------------

@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    """Stage 1: image -> Gemini -> {description, terms[]}."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload.")
    try:
        Image.open(io.BytesIO(raw)).verify()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Not a readable image: {exc}")
    try:
        result = pipeline.analyze_image(raw, file.content_type or "image/png")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Image analysis failed: {exc}")
    return result.model_dump()


@app.post("/api/generate")
def generate(deck: str = Form(...), words: str = Form(...), model: str = Form(DEFAULT_CARD_MODEL)):
    """Stages 2+3: filter out words already in the collection, then generate cards
    only for the genuinely new ones (so we never spend tokens on a duplicate)."""
    seen, targets = set(), []
    for tok in re.split(r"[\s,，、;；]+", words.strip()):
        tok = tok.strip()
        if tok and CJK.search(tok) and tok not in seen:
            seen.add(tok)
            targets.append(tok)
    targets = targets[:MAX_TARGETS]
    if not targets:
        raise HTTPException(status_code=400, detail="No Mandarin words found to generate from.")

    already_set = words_already_present(deck, targets, model)
    already = [w for w in targets if w in already_set]
    new_words = [w for w in targets if w not in already_set]
    known = deck_known_words(deck)

    context = {"targets": targets, "alreadyInDeck": already, "newWords": new_words, "knownCount": len(known)}
    if not new_words:
        return {"cards": [], "context": context}
    try:
        result = pipeline.generate_cards(new_words, known)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Card generation failed: {exc}")
    return {"cards": [c.model_dump() for c in result.cards], "context": context}


def _bold_target(sentence: str, word: str) -> str:
    """Wrap the target word in <b> so it stands out in the sentence field."""
    if word and word in sentence:
        return sentence.replace(word, f"<b>{word}</b>")
    return sentence


def _tts_media(text: str, prefix: str) -> str:
    """Synthesize `text` with Piper, store it in Anki's media, return a
    [sound:...] reference (or '' if TTS/store failed — audio is best-effort)."""
    text = re.sub(r"<[^>]+>", "", text or "").strip()
    if not text:
        return ""
    try:
        r = httpx.post(f"{TTS_URL}/tts", json={"text": text, "speed": 1.0}, timeout=60.0)
        r.raise_for_status()
    except httpx.HTTPError:
        return ""
    fname = f"sa_{prefix}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}.wav"
    try:
        anki_request("storeMediaFile", filename=fname, data=base64.b64encode(r.content).decode())
    except HTTPException:
        return ""
    return f"[sound:{fname}]"


@app.post("/api/add-cards")
def add_cards(deck: str = Form(...), cards: str = Form(...), model: str = Form(DEFAULT_CARD_MODEL)):
    """Stage 4: add selected cards as HSK-model notes (with generated audio) and sync."""
    try:
        items = json.loads(cards)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cards JSON: {exc}")
    if not items:
        raise HTTPException(status_code=400, detail="No cards to add.")

    anki_request("createDeck", deck=deck)

    # Build the notes with text fields only; audio is generated *after* the
    # duplicate check below so we don't waste TTS on cards that won't be added.
    # `metas` keeps (simplified, example) parallel to `notes` for that second pass.
    notes, metas, seen = [], [], set()
    for c in items:
        simp = (c.get("simplified") or "").strip()
        if not simp or simp in seen:  # skip blanks and in-batch duplicates
            continue
        seen.add(simp)
        trad = (c.get("traditional") or "").strip() or simp
        example = (c.get("example") or "").strip()
        notes.append({
            "deckName": deck, "modelName": model,
            "fields": {
                "Key": simp,  # first field must be non-empty; also used for dedup
                "Simplified": simp,
                "Traditional": trad,
                "Pinyin.1": (c.get("pinyin") or "").strip(),
                "Meaning": (c.get("meaning") or "").strip(),
                "Part of speech": (c.get("part_of_speech") or "").strip(),
                "SentenceSimplified": _bold_target(example, simp),
                "SentencePinyin.1": (c.get("example_pinyin") or "").strip(),
                "SentenceMeaning": (c.get("example_meaning") or "").strip(),
                "Audio": "",
                "SentenceAudio": "",
            },
            "options": {"allowDuplicate": False}, "tags": ["screenshot-anki"],
        })
        metas.append((simp, example))
    if not notes:
        raise HTTPException(status_code=400, detail="No valid cards (missing the word).")

    # This AnkiConnect fork's addNotes rejects the WHOLE batch with an error if any
    # note is a duplicate, so pre-filter with canAddNotes and only send the ones
    # that will succeed — duplicates are skipped and reported, never fatal.
    can_add = anki_request("canAddNotes", notes=notes)

    audio_count = 0
    addable = []
    for note, ok, (simp, example) in zip(notes, can_add, metas):
        if not ok:
            continue
        # Generate word + sentence audio via Piper, store in Anki media (best-effort).
        word_audio = _tts_media(simp, "w")
        sent_audio = _tts_media(example, "s")
        audio_count += bool(word_audio) + bool(sent_audio)
        note["fields"]["Audio"] = word_audio
        note["fields"]["SentenceAudio"] = sent_audio
        addable.append(note)

    added = []
    if addable:
        ids = anki_request("addNotes", notes=addable)
        added = [i for i in ids if i]

    sync_error = None
    if added:
        try:
            anki_request("sync")
        except HTTPException as exc:
            sync_error = exc.detail  # not logged in etc. — report, don't fail the add
    return {"requested": len(notes), "added": len(added), "duplicates": len(notes) - len(added),
            "audio_files": audio_count, "sync_error": sync_error}


# --- Standalone utilities (kept from earlier) -----------------------------

@app.post("/api/ocr")
async def ocr(file: UploadFile = File(...), lang: str = Form(DEFAULT_OCR_LANG)):
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


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screenshot → Anki</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="crossorigin" />
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=Noto+Serif+SC:wght@400;600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet" />
<style>
  html, body { margin: 0; padding: 0; background: #f4f1ec; }
  * { box-sizing: border-box; }
  body { font-family: 'Instrument Sans', 'Helvetica Neue', Helvetica, sans-serif; color: #1a1815; min-height: 100vh; display: flex; flex-direction: column; }
  input, textarea, button, select { font-family: inherit; }
  ::placeholder { color: #a89f92; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .hd { font-size: 13px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: #8a8175; margin: 0; }
  .mono { font-family: 'JetBrains Mono', monospace; }
  .btn-primary { font-size: 14px; font-weight: 600; color: #fff; background: oklch(0.52 0.14 258); border: none; border-radius: 10px; padding: 12px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 9px; }
  .btn-primary:hover { background: oklch(0.46 0.14 258); }
  .btn-primary:disabled { opacity: .6; cursor: default; }
  .btn-ghost { font-size: 13px; font-weight: 500; color: #4a443b; background: #fff; border: 1px solid #ded6c9; border-radius: 9px; padding: 10px 14px; cursor: pointer; }
  .btn-ghost:hover { background: #ebe6dd; }
  .spinner { width: 13px; height: 13px; border: 2px solid rgba(255,255,255,0.35); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; }
  input.edit, textarea.edit { border: 1px solid transparent; border-radius: 7px; background: transparent; }
  input.edit:hover, textarea.edit:hover { background: #faf8f4; }
  input.edit:focus, textarea.edit:focus { outline: none; border-color: oklch(0.62 0.14 258); background: #fff; }
  /* On a single-column (phone) layout the Source section stacks ABOVE the cards.
     Its desktop stickiness then pins it over the entire cards column and the page
     is too short to scroll them clear, so the cards stay hidden behind Source.
     Only keep it sticky once there's room for the two-column layout. */
  @media (max-width: 760px) {
    .source-col { position: static !important; top: auto !important; }
  }
</style>
</head>
<body>

<header style="position: sticky; top: 0; z-index: 20; background: rgba(244,241,236,0.92); backdrop-filter: blur(8px); border-bottom: 1px solid #e0d9cd; padding: 12px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;">
  <div style="display: flex; align-items: baseline; gap: 10px; margin-right: auto;">
    <span style="font-size: 16px; font-weight: 600; letter-spacing: -0.01em;">Screenshot → Anki</span>
    <span class="mono" id="model-badge" style="font-size: 11px; color: #8a8175;"></span>
  </div>
  <label style="display: flex; align-items: center; gap: 8px; font-size: 13px; color: #6f675c;">
    Deck
    <select id="deck" style="font-size: 13px; font-weight: 500; color: #1a1815; padding: 7px 10px; border: 1px solid #ded6c9; border-radius: 8px; background: #fff;"></select>
  </label>
  <div style="display: flex; align-items: center; gap: 7px; font-size: 12px; color: #6f675c; padding: 7px 11px; border: 1px solid #ded6c9; border-radius: 999px; background: #fff;">
    <span id="anki-dot" style="width: 7px; height: 7px; border-radius: 50%; background: #c0b9ab;"></span>
    <span id="anki-label">AnkiConnect</span>
  </div>
</header>

<div style="flex: 1; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr)); align-items: start; gap: 18px; padding: 18px 20px 120px; max-width: 1320px; width: 100%; margin: 0 auto;">

  <!-- SOURCE -->
  <section class="source-col" style="background: #fff; border: 1px solid #e6dfd3; border-radius: 14px; padding: 16px; display: flex; flex-direction: column; gap: 14px; position: sticky; top: 74px;">
    <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px;">
      <h2 class="hd">Source</h2>
      <button class="btn-ghost" id="replace-btn" style="padding: 6px 11px; font-size: 12.5px;">Replace</button>
    </div>

    <input type="file" id="file" accept="image/*" style="display: none;">
    <div id="dropzone" style="border: 1.5px dashed #d6cec0; border-radius: 10px; padding: 40px 20px; text-align: center; cursor: pointer; background: #fbfaf7;">
      <div style="font-size: 14.5px; font-weight: 500; margin-bottom: 5px;">Drop a screenshot, or tap to choose</div>
      <div style="font-size: 12.5px; color: #8a8175;">Photo, screenshot, or paste an image from the clipboard</div>
    </div>
    <div id="preview-wrap" style="display: none; position: relative; border-radius: 10px; overflow: hidden; border: 1px solid #e6dfd3; background: #f0ece5;">
      <img id="preview" alt="screenshot" style="display: block; width: 100%; max-height: 320px; object-fit: contain;">
      <span id="preview-tag" class="mono" style="position: absolute; left: 8px; bottom: 8px; font-size: 11px; color: #4a443b; background: rgba(255,255,255,0.85); padding: 5px 9px; border-radius: 6px;"></span>
    </div>

    <div id="desc" style="display: none; font-size: 13px; color: #6f675c; line-height: 1.55; background: #fbfaf7; border: 1px solid #eee6d9; border-radius: 8px; padding: 9px 11px;"></div>

    <div>
      <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 7px;">
        <span style="font-size: 12.5px; font-weight: 500; color: #6f675c;">Mandarin terms</span>
        <span class="mono" style="font-size: 11px; color: #a09789;">one per line, editable</span>
      </div>
      <textarea id="terms" rows="6" placeholder="Analyze an image, or type target words here…" style="width: 100%; resize: vertical; font-family: 'Noto Serif SC', serif; font-size: 15px; line-height: 1.75; color: #1a1815; padding: 12px; border: 1px solid #e6dfd3; border-radius: 10px; background: #fbfaf7;"></textarea>
    </div>

    <button class="btn-primary" id="generate-btn">Extract &amp; generate cards</button>
    <div id="src-msg" style="font-size: 12px; color: #8a8175; min-height: 14px;"></div>
  </section>

  <!-- PROPOSED CARDS -->
  <section style="display: flex; flex-direction: column; gap: 12px;">
    <div style="display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 0 2px;">
      <h2 class="hd">Proposed cards</h2>
      <span id="summary" style="font-size: 12.5px; color: #8a8175;">none yet</span>
    </div>
    <div id="cards"></div>
    <div id="empty" style="border: 1px dashed #ded6c9; border-radius: 14px; padding: 44px 20px; text-align: center; color: #8a8175; font-size: 13.5px;">Nothing yet — add a screenshot, then generate cards.</div>
  </section>
</div>

<!-- ACTION BAR -->
<div style="position: fixed; left: 0; right: 0; bottom: 0; z-index: 30; background: rgba(244,241,236,0.93); backdrop-filter: blur(8px); border-top: 1px solid #e0d9cd; padding: 12px 20px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;">
  <div style="margin-right: auto; display: flex; flex-direction: column; gap: 2px; min-width: 0;">
    <span id="bar-title" style="font-size: 13.5px; font-weight: 500;">0 cards ready</span>
    <span id="bar-sub" style="font-size: 12px; color: #8a8175;">Edit any field before adding — nothing is sent until you confirm</span>
  </div>
  <button class="btn-ghost" id="select-all">Select all</button>
  <button class="btn-primary" id="add-btn" style="padding: 11px 18px;">Add to deck</button>
</div>

<script>
const S = { decks: [], deck: "", cards: [], added: false, analyzing: false, generating: false };
let uid = 0;

const $ = id => document.getElementById(id);
const deckLeaf = () => (S.deck || "deck").split("::").pop();

async function loadStatus() {
  try {
    const d = await (await fetch('/api/anki/status')).json();
    $('anki-dot').style.background = d.connected ? 'oklch(0.62 0.15 150)' : 'oklch(0.62 0.2 25)';
    $('anki-label').textContent = d.connected ? 'AnkiConnect' : 'AnkiConnect offline';
    if (d.connected && d.decks) {
      S.decks = d.decks;
      const sel = $('deck');
      sel.innerHTML = '';
      d.decks.forEach(name => { const o = document.createElement('option'); o.value = o.textContent = name; sel.appendChild(o); });
      const pref = d.decks.find(x => /hsk/i.test(x)) || d.decks.find(x => x !== 'Default') || d.decks[0];
      S.deck = pref; sel.value = pref;
    }
  } catch (e) {
    $('anki-label').textContent = 'AnkiConnect offline';
  }
  fetch('/health');
}

// --- image -> analyze ---
function showPreview(file) {
  $('dropzone').style.display = 'none';
  $('preview-wrap').style.display = 'block';
  $('preview').src = URL.createObjectURL(file);
  $('preview-tag').textContent = file.name + ' · ' + Math.round(file.size / 1024) + ' KB';
}

async function analyze(file) {
  S.analyzing = true; setSrcMsg('Analyzing image…'); refreshGenerate();
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await fetch('/api/analyze', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) { setSrcMsg('Analyze error: ' + (d.detail || r.status)); return; }
    if (d.description) { $('desc').style.display = 'block'; $('desc').textContent = d.description; }
    $('terms').value = (d.terms || []).map(t => t.hanzi + (t.meaning ? '  —  ' + t.meaning : '')).join('\n');
    setSrcMsg((d.terms || []).length + ' term(s) found · edit, then generate');
  } catch (e) { setSrcMsg('Analyze failed: ' + e); }
  finally { S.analyzing = false; refreshGenerate(); }
}

$('file').addEventListener('change', e => { const f = e.target.files[0]; if (f) { showPreview(f); analyze(f); } });
$('dropzone').addEventListener('click', () => $('file').click());
$('replace-btn').addEventListener('click', () => $('file').click());
['dragover','dragenter'].forEach(ev => $('dropzone').addEventListener(ev, e => { e.preventDefault(); $('dropzone').style.borderColor = 'oklch(0.62 0.14 258)'; }));
['dragleave','drop'].forEach(ev => $('dropzone').addEventListener(ev, e => { e.preventDefault(); $('dropzone').style.borderColor = '#d6cec0'; }));
$('dropzone').addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) { showPreview(f); analyze(f); } });
window.addEventListener('paste', e => { const it = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith('image/')); if (it) { const f = it.getAsFile(); showPreview(f); analyze(f); } });

$('deck').addEventListener('change', e => { S.deck = e.target.value; S.added = false; updateBar(); });

function setSrcMsg(t) { $('src-msg').textContent = t; }

// --- generate ---
$('generate-btn').addEventListener('click', async () => {
  const words = $('terms').value.trim();
  if (!words) { setSrcMsg('Add an image or type some Mandarin words first.'); return; }
  S.generating = true; refreshGenerate();
  const fd = new FormData(); fd.append('deck', S.deck); fd.append('words', words);
  try {
    const r = await fetch('/api/generate', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) { setSrcMsg('Generate error: ' + (d.detail || r.status)); return; }
    S.cards = (d.cards || []).map(c => ({ id: ++uid, included: true, ...c }));
    S.added = false;
    const ctx = d.context || {};
    const parts = [];
    if (ctx.newWords) parts.push(ctx.newWords.length + ' new');
    if (ctx.alreadyInDeck && ctx.alreadyInDeck.length) parts.push(ctx.alreadyInDeck.length + ' already in deck');
    parts.push((ctx.knownCount || 0) + ' known-vocab in context');
    setSrcMsg(parts.join(' · '));
    renderCards(); updateBar();
  } catch (e) { setSrcMsg('Generate failed: ' + e); }
  finally { S.generating = false; refreshGenerate(); }
});

function refreshGenerate() {
  const b = $('generate-btn');
  const busy = S.analyzing || S.generating;
  b.disabled = busy;
  const label = S.generating ? 'Generating cards…' : S.analyzing ? 'Analyzing…' : (S.cards.length ? 'Regenerate cards' : 'Extract & generate cards');
  b.innerHTML = (busy ? '<span class="spinner"></span>' : '') + label;
}

// --- proposed cards ---
function renderCards() {
  const wrap = $('cards');
  $('empty').style.display = S.cards.length ? 'none' : 'block';
  wrap.innerHTML = '';
  S.cards.forEach(c => {
    const art = document.createElement('article');
    art.style.cssText = 'background:#fff;border:1px solid #e6dfd3;border-radius:14px;padding:14px 14px 14px 12px;display:grid;grid-template-columns:26px 1fr;gap:12px;margin-bottom:12px;opacity:' + (c.included ? 1 : 0.5);
    const lbl = "font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:0.04em;text-transform:uppercase;color:#a09789;";
    art.innerHTML = `
      <button data-act="toggle" data-id="${c.id}" title="Include" style="width:24px;height:24px;margin-top:3px;border-radius:7px;border:1.5px solid ${c.included ? 'oklch(0.52 0.14 258)' : '#d6cec0'};background:${c.included ? 'oklch(0.52 0.14 258)' : '#fff'};cursor:pointer;color:#fff;font-size:13px;line-height:1;padding:0;">${c.included ? '✓' : ''}</button>
      <div style="display:flex;flex-direction:column;gap:9px;min-width:0;">
        <div style="display:flex;align-items:flex-start;gap:10px;">
          <input class="edit" data-field="simplified" data-id="${c.id}" value="${esc(c.simplified)}" style="flex:1;min-width:0;font-family:'Noto Serif SC',serif;font-size:32px;font-weight:600;line-height:1.2;color:#1a1815;padding:1px 2px;border-bottom:1px solid transparent;" />
          <button data-act="delete" data-id="${c.id}" title="Discard" style="margin-top:10px;width:26px;height:26px;flex:none;border:none;background:transparent;color:#b5aa9a;font-size:17px;cursor:pointer;border-radius:7px;">×</button>
        </div>
        <input class="edit" data-field="pinyin" data-id="${c.id}" value="${esc(c.pinyin)}" placeholder="pinyin" style="font-family:'Gentium Plus',Georgia,serif;font-size:16px;color:#0a7a2f;padding:2px 4px;" />
        <div style="display:grid;grid-template-columns:64px 1fr;align-items:center;gap:8px;">
          <span style="${lbl}">Meaning</span>
          <input class="edit" data-field="meaning" data-id="${c.id}" value="${esc(c.meaning)}" style="width:100%;font-size:15px;color:#1a1815;padding:6px 8px;" />
        </div>
        <div style="display:grid;grid-template-columns:64px 1fr;align-items:center;gap:8px;">
          <span style="${lbl}">Type</span>
          <input class="edit" data-field="part_of_speech" data-id="${c.id}" value="${esc(c.part_of_speech)}" placeholder="part of speech" style="width:100%;font-size:14px;color:#575757;padding:6px 8px;" />
        </div>
        <div style="display:grid;grid-template-columns:64px 1fr;align-items:start;gap:8px;">
          <span style="${lbl}padding-top:9px;">Example</span>
          <div style="display:flex;flex-direction:column;gap:2px;min-width:0;">
            <textarea class="edit" data-field="example" data-id="${c.id}" rows="2" style="width:100%;resize:vertical;font-family:'Noto Serif SC',serif;font-size:16px;line-height:1.7;color:#3d382f;padding:6px 8px;">${esc(c.example)}</textarea>
            <input class="edit" data-field="example_pinyin" data-id="${c.id}" value="${esc(c.example_pinyin)}" placeholder="sentence pinyin" style="font-family:'Gentium Plus',Georgia,serif;font-size:13.5px;color:#0a7a2f;padding:4px 8px;" />
            <textarea class="edit" data-field="example_meaning" data-id="${c.id}" rows="2" style="width:100%;resize:vertical;font-size:13.5px;line-height:1.55;color:#7d756a;padding:5px 8px;">${esc(c.example_meaning)}</textarea>
          </div>
        </div>
      </div>`;
    wrap.appendChild(art);
  });
}

function esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

// edits update state in place (no re-render → no focus loss)
$('cards').addEventListener('input', e => {
  const f = e.target.dataset.field; if (!f) return;
  const c = S.cards.find(x => x.id == e.target.dataset.id); if (c) { c[f] = e.target.value; S.added = false; }
});
$('cards').addEventListener('click', e => {
  const act = e.target.dataset.act; if (!act) return;
  const id = e.target.dataset.id;
  if (act === 'toggle') { const c = S.cards.find(x => x.id == id); if (c) c.included = !c.included; }
  if (act === 'delete') { S.cards = S.cards.filter(x => x.id != id); }
  S.added = false; renderCards(); updateBar();
});

// --- action bar ---
function updateBar() {
  const kept = S.cards.filter(c => c.included).length;
  $('summary').textContent = S.cards.length ? kept + ' of ' + S.cards.length + ' selected' : 'none yet';
  const allOn = kept === S.cards.length && S.cards.length > 0;
  $('select-all').textContent = allOn ? 'Deselect all' : 'Select all';
  if (S.added) {
    $('bar-title').textContent = kept + ' cards added to ' + S.deck;
    $('bar-sub').textContent = S.lastSync || 'Done';
    $('add-btn').textContent = 'Added ✓';
  } else {
    $('bar-title').textContent = kept + (kept === 1 ? ' card' : ' cards') + ' ready';
    $('bar-sub').textContent = 'Edit any field before adding — nothing is sent until you confirm';
    $('add-btn').textContent = 'Add to ' + deckLeaf();
  }
  $('add-btn').disabled = kept === 0;
}

$('select-all').addEventListener('click', () => {
  const allOn = S.cards.filter(c => c.included).length === S.cards.length && S.cards.length > 0;
  S.cards.forEach(c => c.included = !allOn); S.added = false; renderCards(); updateBar();
});

$('add-btn').addEventListener('click', async () => {
  const chosen = S.cards.filter(c => c.included);
  if (!chosen.length) return;
  $('add-btn').disabled = true; $('add-btn').textContent = 'Adding + audio…';
  const fd = new FormData(); fd.append('deck', S.deck);
  fd.append('cards', JSON.stringify(chosen.map(c => ({
    simplified: c.simplified, traditional: c.traditional, pinyin: c.pinyin, meaning: c.meaning,
    part_of_speech: c.part_of_speech, example: c.example, example_pinyin: c.example_pinyin, example_meaning: c.example_meaning
  }))));
  try {
    const r = await fetch('/api/add-cards', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) { $('bar-sub').textContent = 'Add error: ' + (d.detail || r.status); $('add-btn').disabled = false; $('add-btn').textContent = 'Add to ' + deckLeaf(); return; }
    S.added = true;
    let sub = 'Added ' + d.added + (d.duplicates ? ' · ' + d.duplicates + ' duplicate(s) skipped' : '');
    if (d.audio_files) sub += ' · ' + d.audio_files + ' audio clip(s)';
    sub += d.sync_error ? ' · sync failed: ' + d.sync_error : ' · synced with AnkiWeb';
    S.lastSync = sub;
    updateBar();
  } catch (e) { $('bar-sub').textContent = 'Add failed: ' + e; $('add-btn').disabled = false; }
});

$('model-badge').textContent = '';
loadStatus();
refreshGenerate();
updateBar();
fetch('/api/anki/status');
</script>
</body>
</html>"""
