# anki-headless-docker

Headless [Anki](https://apps.ankiweb.net/) in Docker with the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on, plus a small
web app for **OCR**, **text-to-speech**, and a **Gemini** chat box.

**Long-term goal:** photograph text → OCR it → translate it → generate Anki
cards, all against a containerized Anki that syncs to your AnkiWeb account.
**Today:** the three building blocks (OCR, TTS, Gemini) work independently and
the container syncs to AnkiWeb. The OCR→translate→card *chain* is not built yet.

## Services

| Service  | What it is                                  | Host port (loopback only)                  |
|----------|---------------------------------------------|--------------------------------------------|
| `anki`   | Headless Anki + AnkiConnect (X11/VNC image) | `5900` VNC, `8766` AnkiConnect, `3141` MCP |
| `webapp` | FastAPI UI: OCR + TTS + Gemini              | `8000` web UI                              |
| `tts`    | Piper text-to-speech (Chinese voice)        | `8082` → container `8080`                  |

All ports bind to `127.0.0.1` only — nothing is exposed off this machine.

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
├── .env                  # GEMINI_API_KEY / GEMINI_MODEL (gitignored)
├── anki/
│   └── Dockerfile        # bakes AnkiConnect in; env-governed 0.0.0.0 bind; no auto-update
├── webapp/               # FastAPI UI, proxies to anki + tts + Gemini
│   ├── Dockerfile        # python:3.12-slim + Tesseract (eng, chi_sim, chi_tra); deps via uv
│   ├── app.py
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

`prefs21.db`, addons, and the rest of `/data` live in the **named volume
`anki_data`**, not on the host. Only the collection (`user1/`) is bind-mounted.

## Configuration (`.env`)

The Gemini panel needs an API key. Create `.env` in this directory (gitignored —
never commit it):

```
GEMINI_API_KEY=your-google-ai-studio-key
GEMINI_MODEL=gemini-3.5-flash-lite
```

`docker compose` reads this file automatically and injects it into `webapp`.
Without a key, OCR and TTS still work; only "Ask Gemini" is disabled.

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
curl -s -X POST localhost:8000/api/chat -F 'text=say OK'                  # webapp -> Gemini
curl -s -X POST localhost:8000/api/tts  -F 'text=你好' -o out.wav          # webapp -> Piper
```

## Dependency management (uv)

`webapp` and `tts` use [uv](https://docs.astral.sh/uv/). Each has a
`pyproject.toml` + `uv.lock`; the Dockerfiles copy the uv binary from
`ghcr.io/astral-sh/uv:0.11` and run `uv sync --frozen --no-dev` into a project
`.venv` (put on `PATH`). To change deps: edit `pyproject.toml`, run `uv lock` in
that service dir, then rebuild (`docker compose build <service>`).

## AnkiWeb login (via VNC) — and the restart caveat

AnkiConnect's `sync` action carries **no credentials**; it just tells an
already-logged-in profile to sync. Recent Anki no longer lets scripts pass an
email/password, so you log in through the UI once per running container:

1. Connect a VNC client to `localhost:5900` (no password — the image runs VNC
   with `-SecurityTypes None`). TigerVNC / RealVNC Viewer connect cleanly;
   macOS Screen Sharing may prompt for a password anyway — leave it blank.
2. In the Anki window: **Sync**, log in with your AnkiWeb account.
3. Sync now works for the life of this container:
   ```bash
   curl -s localhost:8766 -X POST -d '{"action":"sync","version":6}'
   ```

> ### ⚠️ Known limitation: login does NOT survive a container restart
>
> The sync token is held in Anki's **memory** and is only flushed to
> `prefs21.db` on a *clean* shutdown. `docker restart` / `up --force-recreate` /
> a machine reboot SIGKILL Anki before it flushes, so **after any restart you
> must re-login via VNC**. This is accepted for now — just log back in.
>
> What we ruled out while chasing this: it is *not* a volume-persistence bug
> (`prefs21.db` is in the `anki_data` named volume where writes would persist),
> and it is *not* the single-file-bind-mount issue (that was fixed by moving
> `prefs21.db` off a host bind). The blocker is purely that Anki never performs
> a clean shutdown in the container. `guiExitAnki` did **not** trigger one in
> testing. A real fix would need a clean-quit-on-stop mechanism (e.g. a wrapper
> that closes the Anki window on SIGTERM with an increased `stop_grace_period`),
> which was deliberately deferred.

## Feature notes

### OCR
- Upload an image → Tesseract extracts text (default `chi_sim+eng`, editable per
  request). The page shows a live AnkiConnect status banner (proves webapp→anki
  connectivity).
- Endpoints: `GET /`, `GET /api/anki/status`, `POST /api/ocr`, `GET /health`.

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
  from other containers or the host port. It is made to bind `0.0.0.0` via the
  **`ANKICONNECT_BIND_ADDRESS=0.0.0.0`** env var (this fork reads it in its
  `DEFAULT_CONFIG`). `anki/Dockerfile` **deletes** `webBindAddress` from
  `config.json` (so the env var wins) and **disables addon auto-update**
  (`meta.json` → `update_enabled:false`, so Anki can't re-download AnkiConnect
  and reset the config). Hardcoding `0.0.0.0` in `config.json` was unreliable
  because `/data` is a Docker volume that shadows/rewrites it.
- **Stale X lock on restart** — the base image keeps `/tmp` across a
  `docker restart`, and a leftover `/tmp/.X99-lock` makes Xvnc refuse to start
  ("display 99 already active"), killing VNC. The `anki` service `command:`
  clears `/tmp/.X99-lock` before running `/startup.sh`, so restarts stay clean.
- **Named volume for `/data`** — the base image declares `VOLUME /data`. Using a
  *named* volume (`anki_data`) instead of an anonymous one makes it managed and
  resettable (`docker volume rm anki-headless-docker_anki_data`).

## Security notes

- **The AnkiWeb login is a credential.** It lives in the `anki_data` volume /
  Anki memory. Don't bake it into a shared image. `.env` (Gemini key) and
  `data/` are gitignored.
- The upstream Anki image serves VNC with **no authentication** — that's why
  every port is loopback-bound. Do not publish these ports to `0.0.0.0` or
  forward them through a router without adding auth first.
- This project is **not yet a git repo** (`git init` when ready). `.gitignore`
  is already in place, so `.env` and `data/` won't be staged once it is.
