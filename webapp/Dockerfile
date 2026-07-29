FROM python:3.12-slim

# uv for dependency management (copied from the official image).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# Tesseract OCR engine + language data. Chinese (simplified & traditional)
# because the target Anki decks are Mandarin; English for mixed text.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install deps from the lockfile first (cached until deps change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py .

# Put the project venv on PATH so `uvicorn` resolves to it.
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
