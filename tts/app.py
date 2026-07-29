"""Piper TTS service — POST text, get back WAV audio.

Shells out to the bundled Piper binary (no torch). Single voice for now
(Chinese, Huayan medium); the model reads mixed text but pronounces English
words approximately. Failures are surfaced loudly with Piper's stderr.
"""
import os
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

PIPER_BIN = "/opt/piper/piper"
MODEL = "/voices/zh_CN-huayan-medium.onnx"

app = FastAPI(title="Piper TTS")


class TTSRequest(BaseModel):
    text: str
    speed: float = 1.0  # >1 faster, <1 slower


@app.get("/health")
def health():
    return {"status": "ok", "model": os.path.basename(MODEL)}


@app.post("/tts")
def tts(req: TTSRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text.")
    # Piper's length_scale is inverse of speed (higher = slower).
    length_scale = 1.0 / req.speed if req.speed > 0 else 1.0

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out_path = f.name
    try:
        proc = subprocess.run(
            [PIPER_BIN, "--model", MODEL,
             "--length_scale", str(length_scale),
             "--output_file", out_path],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Piper failed: {proc.stderr.decode('utf-8', 'replace')}",
            )
        with open(out_path, "rb") as fh:
            audio = fh.read()
    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)

    if not audio:
        raise HTTPException(status_code=500, detail="Piper produced no audio.")
    return Response(content=audio, media_type="audio/wav")
