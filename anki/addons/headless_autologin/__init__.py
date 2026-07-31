"""Headless AnkiWeb auto-login + non-interactive sync.

Runs inside Anki's own process (this is why it must be an add-on, not a shell
script or an AnkiConnect call — sync_login / set_sync_key / full_upload_or_download
are methods on the live `mw.col` / `mw.pm` objects, not on AnkiConnect's HTTP API).

On every profile open it:

  1. Patches `aqt.sync.full_sync` so a "conflicting collection" never pops a modal
     dialog that would block a headless container forever. A server-decided
     FULL_DOWNLOAD / FULL_UPLOAD is honoured; a genuinely ambiguous FULL_SYNC
     conflict is resolved in the direction given by ANKIWEB_SYNC_ON_CONFLICT
     (default "download" = AnkiWeb wins). Because this patches the one code path
     every sync goes through, the webapp's existing AnkiConnect `sync` becomes
     non-blocking too — no webapp changes needed.
  2. Logs in from ANKIWEB_USERNAME / ANKIWEB_PASSWORD and stores the sync key.
     Anki only flushes that key to prefs21.db on a clean shutdown (which never
     happens in-container), so re-establishing it each boot is what makes a
     `docker restart` survive without a manual VNC login.
  3. Optionally runs one sync at startup (ANKIWEB_SYNC_ON_START, default on) to
     reconcile immediately after a restart.

Everything is wrapped so a network hiccup can never stop Anki from booting.
All actions print() to stdout, i.e. `docker compose logs anki`.

Config (env vars, set via compose from .env):
  ANKIWEB_USERNAME        AnkiWeb email/login (required for auto-login)
  ANKIWEB_PASSWORD        AnkiWeb password
  ANKIWEB_SYNC_ON_CONFLICT  "download" (default, server wins) | "upload"
  ANKIWEB_SYNC_ON_START     "1"/"true"/... (default) to sync at boot, else "0"

Verified against Anki 26.05 (image headless-anki:addons-v1.4.0).
"""
import os
import traceback

import aqt
import aqt.sync
from aqt import gui_hooks

_TRUTHY = {"1", "true", "yes", "on"}


def _log(msg: str) -> None:
    print(f"[headless_autologin] {msg}", flush=True)


def _conflict_direction() -> str:
    val = (os.environ.get("ANKIWEB_SYNC_ON_CONFLICT") or "download").strip().lower()
    return "upload" if val == "upload" else "download"


# --- non-interactive full-sync (replaces aqt.sync.full_sync) ---------------

def _headless_full_sync(mw, out, on_done):
    """Drop-in for aqt.sync.full_sync with no dialogs.

    Reuses Anki's own dialog-free transfer functions (full_download / full_upload),
    which handle the backup, close/reopen, media and progress correctly.
    """
    server_usn = out.server_media_usn if mw.pm.media_syncing_enabled() else None
    try:
        if out.required == out.FULL_DOWNLOAD:
            _log("server requires a full download — downloading from AnkiWeb")
            aqt.sync.full_download(mw, server_usn, on_done)
        elif out.required == out.FULL_UPLOAD:
            _log("server requires a full upload — uploading to AnkiWeb")
            aqt.sync.full_upload(mw, server_usn, on_done)
        else:
            direction = _conflict_direction()
            _log(
                "conflicting collection — auto-resolving via "
                f"ANKIWEB_SYNC_ON_CONFLICT='{direction}' "
                f"({'local overwrites AnkiWeb' if direction == 'upload' else 'AnkiWeb overwrites local'})"
            )
            if direction == "upload":
                aqt.sync.full_upload(mw, server_usn, on_done)
            else:
                aqt.sync.full_download(mw, server_usn, on_done)
    except Exception:
        _log("full sync failed:")
        traceback.print_exc()
        on_done()


def _install_patch() -> None:
    if getattr(aqt.sync, "_headless_patched", False):
        return
    aqt.sync.full_sync = _headless_full_sync
    aqt.sync._headless_patched = True
    _log("patched aqt.sync.full_sync — sync conflicts will no longer show a dialog")


# --- login + startup sync --------------------------------------------------

def _login(mw) -> bool:
    user = (os.environ.get("ANKIWEB_USERNAME") or "").strip()
    pw = os.environ.get("ANKIWEB_PASSWORD") or ""
    if not user or not pw:
        _log("ANKIWEB_USERNAME/ANKIWEB_PASSWORD not set — leaving any existing login untouched")
        return mw.pm.sync_auth() is not None
    try:
        auth = mw.col.sync_login(user, pw, mw.pm.sync_endpoint())
    except Exception as exc:
        _log(f"sync_login failed: {exc!r} — leaving any existing login untouched")
        return mw.pm.sync_auth() is not None
    mw.pm.set_sync_key(auth.hkey)
    mw.pm.set_sync_username(user)
    _log(f"logged in to AnkiWeb as {user} and stored the sync key")
    return True


def _startup_sync(mw) -> None:
    if (os.environ.get("ANKIWEB_SYNC_ON_START") or "1").strip().lower() not in _TRUTHY:
        _log("ANKIWEB_SYNC_ON_START disabled — not syncing at boot")
        return
    if mw.pm.sync_auth() is None:
        _log("not logged in — skipping startup sync")
        return
    _log("starting headless sync")
    # sync_collection runs the network work in the background via taskman; our
    # patched full_sync handles any conflict without a dialog.
    aqt.sync.sync_collection(mw, on_done=lambda: _log("startup sync finished"))


def _on_profile_open() -> None:
    mw = aqt.mw
    try:
        _install_patch()
        if mw is None:
            _log("no main window available — skipping login/sync")
            return
        _login(mw)
        _startup_sync(mw)
    except Exception:
        _log("startup handler failed:")
        traceback.print_exc()


gui_hooks.profile_did_open.append(_on_profile_open)
