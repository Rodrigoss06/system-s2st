"""make openapi - genera el OpenAPI del Dispatcher (contrato de conexion, seccion 4).

FastAPI ya sabe construirlo desde las rutas reales (`dispatcher.create_app`): es
ejecutable de verdad, no una copia a mano que se puede desincronizar del codigo. Cubre
`POST/DELETE /v1/sessions` y `POST/GET /v1/voices`. `GET /healthz` y `GET /readyz`
NO estan aqui: viven en el Worker (`call_serve.py`, `websockets.serve()` crudo), no en
una app FastAPI -- documentados en prosa en el contrato de Notion, no como rutas de
este OpenAPI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .dispatcher import create_app

DEFAULT_OUT = "docs/openapi.json"


def generate(out_path: str = DEFAULT_OUT) -> Path:
    # Base de datos descartable: generar el esquema no debe tocar la de voces real ni
    # requerir credenciales validas (fish_api_key no se usa para esto).
    app = create_app(
        fish_api_key="unused-for-schema-generation", db_path="out/openapi-schema-scratch.db"
    )
    schema = app.openapi()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def main() -> int:
    out_arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    out = generate(out_arg)
    print(f"openapi -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
