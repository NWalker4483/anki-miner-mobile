# anki-headless-docker

Headless [Anki](https://apps.ankiweb.net/) in Docker with the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on, plus a small
web app for a **Screenshot → Anki** Mandarin flashcard pipeline (and standalone
**OCR**, **text-to-speech**, and a **Gemini** chat box).

**The main flow — Screenshot → Anki — is built:** drop an image, Gemini reads
it and pulls out the Mandarin vocabulary (single words plus the compounds and
sub-phrases needed to understand the message), the app filters out words already
in your collection, Gemini drafts cards for the new ones — **grounded in a
bundled CC-CEDICT dictionary** so pinyin/meaning come from real definitions, not
guesses — you edit/select, and they're added to Anki and synced to AnkiWeb.
**AnkiWeb login is headless and automatic** (no VNC needed after a restart).

## Services

| Service  | What it is                                  | Host port                                            |
|----------|---------------------------------------------|------------------------------------------------------|
| `anki`   | Headless Anki + AnkiConnect (X11/VNC image) | `5900` VNC, `8766` AnkiConnect, `3141` MCP — loopback |
| `webapp` | FastAPI UI + Screenshot→Anki pipeline       | `8000` web UI — **`0.0.0.0` (LAN-exposed, see below)** |
| `tts`    | Piper text-to-speech (Chinese voice)        | `8082` → container `8080` — loopback                 |

All `anki` and `tts` ports bind to `127.0.0.1` only. **`webapp:8000` is bound to
`0.0.0.0`** so it can be opened from a phone on the same Wi-Fi
(`http://<this-machine-ip>:8000`). The app has **no authentication**, so only do
this on a trusted network — to lock it back down, change the webapp port in
`docker-compose.yaml` back to `127.0.0.1:8000:8000` and recreate.

### Port note: 8766 vs 8765

Inside the `anki` container AnkiConnect listens on **8765**. On the **host** it
is published as **8766**, because the local Anki desktop app already owns 8765.

- From your Mac: `http://localhost:8766`
- From another container on the compose network: `http://anki:8765` (service
  name + *container* port — the host remap does not apply inside the network).

## Layout

```
.
├── docker-compose.yaml   # 3 services + shared network + named volume `anki_data`
├── .env                  # Gemini + AnkiWeb secrets (gitignored)
├── LICENSE               # GNU AGPL-3.0
├── anki/
│   ├── Dockerfile        # bakes AnkiConnect in; env-governed 0.0.0.0 bind; no auto-update
│   └── addons/
│       └── headless_autologin/   # mounted add-on: env login + non-interactive sync
├── webapp/               # FastAPI UI + Screenshot→Anki pipeline
│   ├── Dockerfile        # python:3.12-slim + Tesseract (eng, chi_sim, chi_tra); deps via uv
│   ├── app.py            # routes, AnkiConnect calls, the UI
│   ├── pipeline.py       # Pydantic contracts + Gemini calls (google-genai SDK)
│   ├── cedict.py         # CC-CEDICT loader/lookup (grounds card generation)
│   ├── cedict_ts.u8      # bundled CC-CEDICT dictionary (~9.4 MB, CC BY-SA)
│   ├── pyproject.toml    # deps (managed by uv)
│   └── uv.lock
├── tts/                  # Piper TTS service (no torch)
│   ├── Dockerfile        # Piper binary + Chinese voice baked in; deps via uv
│   ├── app.py
│   ├── pyproject.toml
│   └── uv.lock
└── data/                 # host-visible Anki data (gitignored)
    └── user1/            # the collection (.anki2), bind-mounted for backup
```

`prefs21.db`, add-ons, and the rest of `/data` live in the **named volume
`anki_data`**, not on the host. Only the collection (`user1/`) is bind-mounted.
The `headless_autologin` add-on is bind-mounted from `anki/addons/` into
`/data/addons21/` (so the base image needs no rebuild for it).

## Configuration (`.env`)

Create `.env` in this directory (gitignored — never commit it):

```
# Gemini (the analyze/generate/chat features)
GEMINI_API_KEY=your-google-ai-studio-key
GEMINI_MODEL=gemini-3.5-flash-lite

# AnkiWeb headless auto-login (used by the headless_autologin add-on)
ANKIWEB_USERNAME=you@example.com
ANKIWEB_PASSWORD=your-ankiweb-password
# Conflict resolution on an ambiguous "conflicting collection":
#   download = AnkiWeb overwrites the container (default, server wins)
#   upload   = the container overwrites AnkiWeb (local wins)
ANKIWEB_SYNC_ON_CONFLICT=download
# Sync once at container boot to reconcile after a restart (1 = on, 0 = off).
ANKIWEB_SYNC_ON_START=1
```

`docker compose` reads this file automatically. Without `GEMINI_API_KEY`, OCR and
TTS still work; only Gemini features are disabled. Without the `ANKIWEB_*` creds,
the add-on no-ops and you can still log in manually via VNC (see below).

## Quick start

```bash
docker compose up -d --build
```

- Web UI: <http://localhost:8000>
- AnkiConnect (container): <http://localhost:8766>

```bash
# sanity checks
curl -s localhost:8766 -X POST -d '{"action":"deckNames","version":6}'   # anki
curl -s localhost:8000/api/anki/status                                    # webapp -> anki
curl -s "localhost:8000/api/define?word=%E8%A1%8C"                        # CC-CEDICT lookup (行)
curl -s -X POST localhost:8000/api/chat -F 'text=say OK'                  # webapp -> Gemini
curl -s -X POST localhost:8000/api/tts  -F 'text=你好' -o out.wav          # webapp -> Piper
```

## Dependency management (uv)

`webapp` and `tts` use [uv](https://docs.astral.sh/uv/). Each has a
`pyproject.toml` + `uv.lock`; the Dockerfiles copy the uv binary from
`ghcr.io/astral-sh/uv:0.11` and run `uv sync --frozen --no-dev` into a project
`.venv` (put on `PATH`). To change deps: edit `pyproject.toml`, run `uv lock` in
that service dir, then rebuild (`docker compose build <service>`).

## AnkiWeb login (headless & automatic)

Login is handled by the **`headless_autologin` add-on** (`anki/addons/`, mounted
into the container via compose — no rebuild). Modern Anki no longer lets scripts
pass an email/password to `sync`, and `sync_login`/`full_upload_or_download` only
exist on Anki's live objects (not on AnkiConnect's HTTP API), so this logic runs
*inside* Anki as an add-on. On every profile open it:

1. **Re-establishes the login** from `ANKIWEB_USERNAME`/`ANKIWEB_PASSWORD` and
   stores the sync key. Anki only flushes that key to `prefs21.db` on a clean
   shutdown (which never happens in-container), so doing this each boot is what
   makes a `docker restart` survive **without a manual VNC login**.
2. **Patches `aqt.sync.full_sync`** so a "conflicting collection" auto-resolves
   per `ANKIWEB_SYNC_ON_CONFLICT` (default `download` = AnkiWeb wins) instead of
   blocking on a modal dialog. This covers the webapp's AnkiConnect `sync` too.
3. **Runs one sync at boot** (`ANKIWEB_SYNC_ON_START`) to reconcile after a
   restart.

Add-on logs: `docker compose logs anki | grep headless_autologin`.

> ⚠️ **Conflict resolution is destructive by design.** On an *ambiguous* full-sync
> conflict (rare — mostly note-type/schema changes), `download` discards local
> changes and `upload` overwrites AnkiWeb. Pick the direction that matches which
> side you treat as the source of truth.

**Manual fallback (VNC):** if you don't set the `ANKIWEB_*` creds, connect a VNC
client to `localhost:5900` (no password — the image runs VNC with
`-SecurityTypes None`), click **Sync** in the Anki window, and log in. This lasts
the life of the container (and, unlike before, is no longer *required* after a
restart once the add-on is configured).

## Screenshot → Anki pipeline (the main feature)

The home page (`/`) is a "Screenshot → Anki" workflow. Every LLM interaction
uses JSON structured output whose schema is a **Pydantic model** passed to the
`google-genai` SDK as `response_schema` (see `webapp/pipeline.py`).

Four stages:

1. **Analyze** — `POST /api/analyze` (image) → Gemini (multimodal) →
   `ImageAnalysis{ description, terms: [MandarinTerm{hanzi, pinyin, meaning}] }`.
   The prompt assumes the learner can't read the screen and extracts the
   vocabulary needed to understand it — single words plus compounds and common
   sub-phrases, not just isolated characters. The UI fills the editable "Mandarin
   terms" box from this.
2. **Duplicate filter** — inside `POST /api/generate`: `canAddNotes` decides
   which target words Anki would actually accept (note-type first field,
   **collection-wide** — the same rule enforced on add), so words already present
   are dropped *before* generation. It also samples up to 30 existing cards as
   "vocabulary the learner already knows".
3. **Generate** — `POST /api/generate` (`deck`, `words`) → Gemini →
   `GeneratedCards{ cards: [GeneratedCard{simplified, traditional, pinyin,
   meaning, part_of_speech, example, example_pinyin, example_meaning}] }`.
   Each new word is **grounded in CC-CEDICT**: its real readings and senses are
   fed to the model as the source of truth (it picks the context-appropriate
   sense and renders tone-mark pinyin), and example sentences lean on the known
   vocabulary. Words not in the dictionary fall back to the model's own knowledge.
4. **Add** — `POST /api/add-cards` (`deck`, `cards` JSON) → filters duplicates
   with `canAddNotes`, then for each *addable* card synthesizes **word + sentence
   audio** via the `tts` service, stores it in Anki media (`storeMediaFile`),
   `addNotes`, and `sync`. Duplicates are skipped and reported (this AnkiConnect
   fork errors the whole batch otherwise); sync failures are reported, not fatal.

Cards are added as the **HSK note type** (matching the user's HSK1 deck): big
`Simplified`, `Pinyin.1`, `Meaning`, `Part of speech`, the example in
`SentenceSimplified` (target word bolded) with `SentencePinyin.1` /
`SentenceMeaning`, and `Audio` / `SentenceAudio` `[sound:…]` from Piper. The note
type is `CARD_MODEL` (default `HSK`; its first field `Key` is used for dedup).
Gemini model is `GEMINI_MODEL` (default `gemini-3.5-flash-lite`), used for both
vision and generation.

## Feature notes

### CC-CEDICT grounding
- `webapp/cedict.py` parses the bundled `cedict_ts.u8` once into an in-memory
  index keyed by both simplified and traditional headwords → readings/senses (a
  single hanzi like 行 has several readings, each with several senses).
- Card generation feeds a word's real entries into the prompt so pinyin/meaning
  are authoritative. A missing dictionary degrades gracefully (grounding no-ops).
- Inspect a headword: `GET /api/define?word=行`.

### OCR
- Upload an image → Tesseract extracts text (default `chi_sim+eng`, editable per
  request). The page shows a live AnkiConnect status banner (proves webapp→anki
  connectivity).
- Endpoints: `GET /`, `GET /api/anki/status`, `GET /api/define`, `POST /api/ocr`,
  `GET /health`.

### TTS
- `tts/` runs [Piper](https://github.com/rhasspy/piper) — fast local neural TTS,
  no torch. Chinese voice `zh_CN-huayan-medium` baked into the image (offline).
  Single-language: it reads Mandarin well but pronounces embedded English words
  approximately.
- Web app **Text to speech** panel → `POST /api/tts` (form `text`, `speed`) →
  proxies to `tts` (`POST /tts`, JSON `{text, speed}`) → `audio/wav`.
- Add voices later: drop another `.onnx` + `.onnx.json` into the `tts` image and
  parameterize the model path. MeloTTS could be a second engine behind the same
  proxy if native ZH+EN mixing is needed.

### Gemini
- Web app **Ask Gemini** panel → `POST /api/chat` (form `text`) → `{model, reply}`.
  Default model `gemini-3.5-flash-lite`. Backend calls Google with an
  `x-goog-api-key` **header** (never a URL param). Change the model via
  `GEMINI_MODEL` in `.env` then `docker compose up -d webapp`.

## Implementation gotchas (why the config looks the way it does)

- **AnkiConnect bind address** — AnkiConnect defaults to `127.0.0.1`, unreachable
  from other containers or the host port. The container now **self-heals** this
  configuration: every time the `anki` container starts, a `docker-compose.yaml`
  command uses `jq` to enforce `webBindAddress: "0.0.0.0"` and whitelist the
  webapp's origin in `config.json` inside the container. This persists across 
  restarts and ensures the service is reachable even if the config file is 
  reset by the add-on.
- **Add-on loading** — The `anki` container expects all add-ons to reside in
  `./anki/addons/` on the host, which is bind-mounted to `/data/addons21/` in the
  container. This ensures add-ons like AnkiConnect (ID 2055492159) are loaded
  correctly and are persistent. AnkiConnect's `auto-update` is explicitly
  disabled in `meta.json` to prevent it from overwriting the patched `config.json`.
- **Named volume for `/data`** — the base image declares `VOLUME /data`. Using a
  *named* volume (`anki_data`) instead of an anonymous one makes it managed and
  resettable (`docker volume rm anki-headless-docker_anki_data`).
- **`prefs21.db` off host bind** — it must stay in the named volume, not a
  single-file host bind mount: Anki saves the profile via write-temp-then-rename,
  which can't replace a bind-mounted file, so the login would be lost. (With the
  auto-login add-on this matters less, but the layout is kept.)

## Security notes

- **AnkiWeb credentials live only in `.env`** (gitignored), injected into the
  `anki` container as env vars. The stored sync key lives in the `anki_data`
  volume. Don't bake either into a shared image.
- **`webapp:8000` is LAN-exposed (`0.0.0.0`) with no authentication.** Anyone on
  the same network can use it (and spend Gemini credits / modify your Anki).
  Revert to `127.0.0.1:8000:8000` in `docker-compose.yaml` when you don't need
  phone access. The `anki` (VNC has no auth) and `tts` ports stay loopback-only —
  don't publish those to `0.0.0.0` or forward them through a router without auth.
- `.gitignore` keeps `.env` (Gemini key, AnkiWeb creds) and `data/` out of git.

## License

This project is licensed under the **GNU Affero General Public License v3.0** —
see [LICENSE](LICENSE). AGPL is used because the bundled Anki add-on runs against
Anki, which is itself AGPL-licensed.

The bundled dictionary `webapp/cedict_ts.u8` is **CC-CEDICT**, licensed
**CC BY-SA 4.0** by MDBG (license header preserved at the top of the file).
