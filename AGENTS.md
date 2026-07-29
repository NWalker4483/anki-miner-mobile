# AGENTS.md

Orientation for AI agents working on this project. Full detail: [README.md](README.md).

## What this is

A Docker Compose stack toward an **OCR → translate → Anki-card** pipeline:

- `anki` — headless Anki + AnkiConnect, syncs to a real AnkiWeb account.
- `webapp` — FastAPI UI with three working, independent features: OCR (Tesseract),
  TTS (proxies to `tts`), and Gemini chat.
- `tts` — Piper text-to-speech (Chinese voice, no torch).

The three features work; the pipeline that *chains* them (OCR an image →
translate via Gemini → create a card via AnkiConnect `addNote`) is **not built
yet** and is the obvious next task.

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
  `uv lock` in that dir, rebuild. No `requirements.txt`.
- **Gemini key** comes from `.env` (`GEMINI_API_KEY`, `GEMINI_MODEL`), gitignored.
  Passed to `webapp` by compose. Backend calls Gemini via `x-goog-api-key`
  header — never put the key in a URL or commit it.
- **AnkiConnect bind** is `0.0.0.0` via the `ANKICONNECT_BIND_ADDRESS` env var,
  not `config.json` (see README "Implementation gotchas"). Don't re-add
  `webBindAddress` to `config.json` or re-enable addon auto-update.
- **`docker restart` loses the AnkiWeb login** (Anki only flushes the token on a
  clean shutdown, which never happens in-container). Re-login via VNC
  (`localhost:5900` → Sync) after any restart. Accepted limitation — don't rabbit
  -hole on it unless asked.
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

- ✅ Container + AnkiConnect + AnkiWeb sync (re-login needed after restart)
- ✅ OCR (Tesseract: eng, chi_sim, chi_tra)
- ✅ TTS (Piper, Chinese voice)
- ✅ Gemini chat (`gemini-3.5-flash-lite`)
- ⬜ OCR → translate → card-creation chain (next up)
