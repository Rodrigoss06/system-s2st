# SINCRO Engine v4 - Worker (WebSocket + CallSession)
#
# El Worker sirve el stream de audio por WebSocket crudo en puerto 8080.
# Expone GET /healthz y GET /readyz. No es el Dispatcher (FastAPI).
#
# Build:
#   docker build -t sincro-worker .
#
# Run local (necesita .env con credenciales):
#   docker run --rm -p 8080:8080 --env-file .env sincro-worker
#
# El turn-detector (ONNX) se descarga en la imagen, no en runtime:
# asi el arranque en frio de Container Apps no espera descarga de red.

FROM python:3.13-slim AS base

# ---- capa de sistema ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# libgomp1 es dependencia de ONNX Runtime (turn-detector). Sin ella,
# load_eou() falla con un ImportError oscuro sobre una .so de ONNX.

# ---- capa de build ----
FROM base AS builder

# uv es mas rapido que pip y respeta el lockfile implicito de pyproject.toml
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copiar solo los metadatos primero para cachear la capa de dependencias
COPY pyproject.toml .
COPY src/sincro/__init__.py src/sincro/__init__.py

# Instalar dependencias en un venv del sistema
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python -e ".[dev]" && \
    # Bajar modelos ONNX del turn-detector (Silero VAD + EOU)
    /opt/venv/bin/python -m livekit.agents download-files

# ---- capa final ----
FROM base AS final

WORKDIR /app

# Copiar el venv completo con dependencias y modelos
COPY --from=builder /opt/venv /opt/venv

# Copiar el codigo fuente
COPY src/ src/
COPY pyproject.toml .

# Instalar el paquete en modo editable dentro del venv copiado
RUN /opt/venv/bin/pip install -e ".[dev]" --no-deps

# El turn-detector descarga sus modelos a ~/.local/share/livekit/agents/
# por defecto. Los copiamos desde el builder.
COPY --from=builder /root/.local/share /root/.local/share

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Puerto del WebSocket (contrato, seccion 4)
EXPOSE 8080

# Health check: el endpoint GET /healthz responde 200 si el proceso acepta
# conexiones. Usamos el propio websockets para no depender de curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

# Entry point: el Worker (call_serve.py) con su __main__ inline.
# No usamos ws_serve.py (G0, una sola direccion sin emparejar).
CMD ["python", "-m", "sincro.call_serve", "--host", "0.0.0.0", "--port", "8080"]