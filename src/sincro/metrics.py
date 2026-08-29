"""S1/S2 - metricas Prometheus (contrato de conexion, seccion 1: puerto 9090, interno).

Cada servicio (Dispatcher, Worker) corre su propio `prometheus_client` en su propia
Container App: no comparten proceso, asi que no comparten registro. `start_http_server`
levanta un servidor WSGI minimo en un hilo aparte -- no hace falta un framework nuevo
ni tocar el puerto 8080 publico.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, start_http_server

METRICS_PORT: int = 9090

# --- Worker ---
worker_sessions_total = Counter(
    "sincro_worker_sessions_total", "Llamadas que llegaron a emparejar y arrancar"
)
worker_sessions_active = Gauge(
    "sincro_worker_sessions_active", "Llamadas en curso ahora mismo en este worker"
)
worker_tokens_rejected_total = Counter(
    "sincro_worker_tokens_rejected_total", "hello rechazado: token invalido, expirado o reusado"
)
worker_peer_timeouts_total = Counter(
    "sincro_worker_peer_timeouts_total", "Sesiones que expiraron esperando al segundo participante"
)

# --- Dispatcher ---
dispatcher_sessions_created_total = Counter(
    "sincro_dispatcher_sessions_created_total", "POST /v1/sessions atendidos"
)
dispatcher_sessions_deleted_total = Counter(
    "sincro_dispatcher_sessions_deleted_total", "DELETE /v1/sessions/{id} atendidos"
)
dispatcher_voices_enrolled_total = Counter(
    "sincro_dispatcher_voices_enrolled_total", "POST /v1/voices que terminaron en 201"
)
dispatcher_voices_rejected_total = Counter(
    "sincro_dispatcher_voices_rejected_total",
    "POST /v1/voices rechazados (sin consent, lang invalido)",
)


def serve_metrics(port: int = METRICS_PORT) -> None:
    """Llamar una vez al arrancar el proceso (Dispatcher o Worker)."""
    start_http_server(port)
