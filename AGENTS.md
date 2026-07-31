# AGENTS.md

Orientation for AI agents working on this project. Full detail: [README.md](README.md).

## What this is

A Docker Compose stack implementing a **Screenshot → Anki** flashcard pipeline:

- `anki` — headless Anki + AnkiConnect, syncs to a real AnkiWeb account.
- `webapp` — FastAPI UI + the pipeline (image → Gemini analysis → deck search →
  Gemini card generation → AnkiConnect add + sync). Also standalone OCR
  (Tesseract), TTS (proxy to `tts`), and a plain Gemini chat endpoint.
- `tts` — Piper text-to-speech (Chinese voice, no torch).

The pipeline is **built and working end to end** (home page at `/`). All LLM I/O
is JSON validated by Pydantic models via the `google-genai` SDK — see
`webapp/pipeline.py`. Pipeline endpoints: `POST /api/analyze`,
`POST /api/generate`, `POST /api/add-cards`.

## Run & verify

```bash
docker compose up -d --build          # start everything
docker compose ps                     # all three should be Up
```

```bash
curl -s localhost:8766 -X POST -d '{"action":"deckNames","version":6}'  # anki
curl -s localhost:8000/api/anki/status                                   # webapp -> anki (network)
curl -s -X POST localhost:8000/api/ocr  -F 'file=@some.png' -F 'lang=eng'
curl -s -X POST localhost:8000/api/tts  -F 'text=你好' -o out.wav
curl -s -X POST localhost:8000/api/chat -F 'text=say OK'                  # needs GEMINI_API_KEY
```

Web UI: <http://localhost:8000>.

## Things you must know before editing

- **Ports:** host `8766` → container `8765` for AnkiConnect (local Anki desktop
  owns 8765). Inside the compose network use `http://anki:8765`, not 8766.
- **Deps are managed with uv** (`webapp`, `tts`): edit `pyproject.toml`, run
  `uv lock` in that dir, rebuild. No `requirements.txt`. The webapp Dockerfile
  copies `app.py`, `pipeline.py`, `cedict.py` and the `cedict_ts.u8` data file —
  add new modules/data to that COPY line.
- **Card generation is grounded in CC-CEDICT** (`webapp/cedict.py` + bundled
  `webapp/cedict_ts.u8`, ~9.4MB, committed). The file is parsed once into an
  in-memory index (simplified + traditional headwords → readings/senses); each
  target word's real dictionary entries are fed into the generation prompt so
  pinyin/meaning are authoritative, not guessed. A missing dictionary degrades
  gracefully (grounding becomes a no-op). Debug lookup: `GET /api/define?word=行`.
  CC-CEDICT is **CC BY-SA 4.0** — keep the license header in the `.u8` file.
- **LLM I/O is JSON via Pydantic** — define contracts as Pydantic models in
  `pipeline.py` and pass them as `response_schema` to the `google-genai` SDK.
  The SDK client is a module-level singleton (a per-call client gets GC'd
  mid-request and closes its httpx pool).
- **Gemini key** comes from `.env` (`GEMINI_API_KEY`, `GEMINI_MODEL`), gitignored.
  Passed to `webapp` by compose. Backend calls Gemini via `x-goog-api-key`
  header — never put the key in a URL or commit it.
- **AnkiConnect bind** is `0.0.0.0` via the `ANKICONNECT_BIND_ADDRESS` env var,
  not `config.json` (see README "Implementation gotchas"). Don't re-add
  `webBindAddress` to `config.json` or re-enable addon auto-update.
- **AnkiWeb login is automated headlessly** by the `headless_autologin` add-on
  (`anki/addons/headless_autologin/`, mounted into `/data/addons21/` via compose —
  no rebuild). It re-establishes the login from `ANKIWEB_USERNAME`/`ANKIWEB_PASSWORD`
  (in `.env`) on every profile open, so a `docker restart` no longer needs a manual
  VNC login. It also patches `aqt.sync.full_sync` so a "conflicting collection"
  auto-resolves per `ANKIWEB_SYNC_ON_CONFLICT` (default `download` = AnkiWeb wins)
  instead of blocking on a modal — this covers the webapp's AnkiConnect `sync` too.
  If `.env` has no creds it no-ops and leaves any existing login untouched (VNC
  fallback: `localhost:5900` → Sync). Add-on logs: `docker compose logs anki`.
- **Don't run blanket destructive Docker commands** (e.g. `docker volume prune`,
  `system prune`). Target this project's resources by name. Other projects share
  this Docker host.

## Adding a card (for the eventual pipeline)

```bash
curl -s localhost:8766 -X POST -d '{
  "action":"addNote","version":6,
  "params":{"note":{"deckName":"Screenshots","modelName":"Basic",
    "fields":{"Front":"...","Back":"..."},"options":{"allowDuplicate":false},
    "tags":["auto"]}}}'
# then sync:
curl -s localhost:8766 -X POST -d '{"action":"sync","version":6}'
```

## Current status

- ✅ Container + AnkiConnect + AnkiWeb sync. Headless auto-login + non-interactive
  conflict resolution via the `headless_autologin` add-on (no VNC re-login needed).
- ✅ Screenshot → Anki pipeline: analyze → deck search → generate → add + sync.
  Cards use the **HSK note type** (big hanzi, pinyin, meaning, POS, example +
  sentence pinyin/meaning) and get **Piper word + sentence audio** stored in
  Anki media on add. Note type = `CARD_MODEL` env (default `HSK`).
- ✅ CC-CEDICT grounding: card pinyin/meaning sourced from the bundled dictionary
  (`GET /api/define?word=…` to inspect). Handles multiple readings/senses per hanzi.
- ✅ OCR (Tesseract: eng, chi_sim, chi_tra)
- ✅ TTS (Piper, Chinese voice)
- ✅ Gemini chat (`gemini-3.5-flash-lite`)
- Ideas next: pinyin/audio on cards (reuse `tts`), richer note types, batch review
