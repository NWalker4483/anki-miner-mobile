FROM python:3.11-slim

# uv for dependency management (copied from the official image).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

# Runtime libs the Piper binary + bundled onnxruntime need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Piper release binary (bundles espeak-ng-data + onnxruntime; no torch).
# Arch-detected so it builds on both Apple Silicon (arm64) and x86_64 hosts.
RUN arch="$(uname -m)"; \
    case "$arch" in \
        aarch64|arm64) A=aarch64 ;; \
        x86_64|amd64)  A=x86_64 ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/piper.tar.gz \
        "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_${A}.tar.gz" \
    && tar -xzf /tmp/piper.tar.gz -C /opt \
    && rm /tmp/piper.tar.gz
ENV LD_LIBRARY_PATH=/opt/piper

# Chinese voice model (Huayan, medium quality). Baked in so runtime is offline.
RUN mkdir -p /voices; \
    base="https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium"; \
    curl -fsSL -o /voices/zh_CN-huayan-medium.onnx      "$base/zh_CN-huayan-medium.onnx?download=true"; \
    curl -fsSL -o /voices/zh_CN-huayan-medium.onnx.json "$base/zh_CN-huayan-medium.onnx.json?download=true"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install deps from the lockfile first (cached until deps change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py .

# Put the project venv on PATH so `uvicorn` resolves to it.
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
