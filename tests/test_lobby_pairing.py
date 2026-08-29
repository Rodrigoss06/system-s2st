"""Prueba de regresion del Lobby (call_serve.py), sin audio ni llamadas a APIs.

Dos casos que no existian antes de la correccion del bug critico de TAREA 1:

1. A conecta, B conecta a los 5 s (mismo session_id), la llamada dura mas de 90 s: A NO
   se debe desconectar (el bug original lo desconectaba a los 60 s exactos, en plena
   llamada).
2. A conecta con un token invalido, B conecta despues con uno valido: ninguna conexion
   queda colgada. (G2 cerro D52: el hello ya no lleva `lang` suelto, lleva un token
   firmado -- el caso de entrada invalida paso de "lang mal formado" a "token invalido",
   que es la superficie real ahora.)

`CallSession` se reemplaza por un doble que solo duerme -- estas pruebas verifican la
mecanica de emparejamiento/timeout del Lobby, no el motor de doblaje. Rapido y gratis:
no toca Deepgram, Groq ni Fish.

Uso: .venv/bin/python tests/test_lobby_pairing.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

import sincro.call_serve as call_serve
from sincro.config import load_settings
from sincro.telemetry import TelemetryWriter
from sincro.tokens import issue_token

FAKE_CALL_DURATION_S = 95.0
PORT_1 = 8767
PORT_2 = 8768


class _FakeCallSession:
    """Doble de CallSession: nada de motores reales, solo simula que la llamada dura.

    Manda `ready` a los dos participantes al arrancar, como la CallSession real -- sin
    esto el cliente que se une segundo se queda esperando un mensaje que nunca llega,
    y el problema aparenta ser del Lobby cuando es solo que el doble esta incompleto.
    """

    def __init__(self, base: object, participant_a: object, participant_b: object, writer: object,
                 on_turn_ab: object = None, on_turn_ba: object = None) -> None:
        self._a = participant_a
        self._b = participant_b

    async def run(self) -> None:
        await self._a.connection.send(json.dumps({"t": "ready", "peer_lang": self._b.lang}))
        await self._b.connection.send(json.dumps({"t": "ready", "peer_lang": self._a.lang}))
        await asyncio.sleep(FAKE_CALL_DURATION_S)


async def test_late_peer_does_not_disconnect_first() -> None:
    """TAREA 1: A no debe caerse a los 60s aunque B tarde en llegar y la llamada siga."""
    call_serve.CallSession = _FakeCallSession  # type: ignore[misc,assignment]
    base = load_settings()
    writer = TelemetryWriter()
    server = await call_serve.run_call_server("localhost", PORT_1, base, writer)
    session_id = "s_test_late_peer"
    try:
        async with connect(f"ws://localhost:{PORT_1}", compression=None) as ws_a:
            token_a = issue_token(session_id, "u_a", "en", "")
            await ws_a.send(json.dumps({"t": "hello", "token": token_a}))
            msg = json.loads(await ws_a.recv())
            assert msg["t"] == "waiting_for_peer", f"esperaba waiting_for_peer, llego {msg}"

            async def join_b_later() -> None:
                await asyncio.sleep(5.0)
                async with connect(f"ws://localhost:{PORT_1}", compression=None) as ws_b:
                    token_b = issue_token(session_id, "u_b", "es", "")
                    await ws_b.send(json.dumps({"t": "hello", "token": token_b}))
                    msg_b = json.loads(await ws_b.recv())
                    assert msg_b["t"] == "ready", f"esperaba ready, llego {msg_b}"
                    await asyncio.sleep(FAKE_CALL_DURATION_S - 5.0 - 5.0)

            b_task = asyncio.create_task(join_b_later())

            t0 = time.monotonic()
            await asyncio.sleep(70.0)  # mas de los 60s del bug original
            elapsed = time.monotonic() - t0
            assert ws_a.state == State.OPEN, (
                f"A se desconecto a los {elapsed:.1f}s (el bug original lo hacia a los 60s)"
            )
            print(f"  OK: A sigue conectado a los {elapsed:.1f}s (> 60s del bug original)")

            await asyncio.sleep(FAKE_CALL_DURATION_S - 70.0 + 2.0)
            assert ws_a.state == State.OPEN, "A se desconecto antes de que la llamada terminara"
            print(f"  OK: A sigue conectado hasta el final de la llamada ({FAKE_CALL_DURATION_S}s)")
            await b_task
    finally:
        server.close()
        await server.wait_closed()


async def test_invalid_token_does_not_hang_either_side() -> None:
    """TAREA 2/3 (G1) + D52 (G2): token invalido de A no debe colgar ni a A ni a B."""
    call_serve.CallSession = _FakeCallSession  # type: ignore[misc,assignment]
    base = load_settings()
    writer = TelemetryWriter()
    server = await call_serve.run_call_server("localhost", PORT_2, base, writer)
    try:
        async with connect(f"ws://localhost:{PORT_2}", compression=None) as ws_a:
            await ws_a.send(json.dumps({"t": "hello", "token": "esto-no-es-un-jwt-valido"}))
            try:
                await asyncio.wait_for(ws_a.recv(), timeout=5.0)
                raised = False
            except ConnectionClosed:
                raised = True
            except TimeoutError:
                raised = False
            assert raised, "A con token invalido deberia cerrarse, no quedarse esperando"
            print("  OK: A con token invalido fue cerrado por el servidor, sin colgarse")

        async with connect(f"ws://localhost:{PORT_2}", compression=None) as ws_b:
            token_b = issue_token("s_test_invalid_token", "u_b", "es", "")
            await ws_b.send(json.dumps({"t": "hello", "token": token_b}))
            msg_b = await asyncio.wait_for(ws_b.recv(), timeout=5.0)
            parsed = json.loads(msg_b)
            assert parsed["t"] == "waiting_for_peer", f"esperaba waiting_for_peer, llego {parsed}"
            print("  OK: B conecta despues sin heredar nada del intento fallido de A")
    finally:
        server.close()
        await server.wait_closed()


async def main() -> int:
    print("test 1: A no se desconecta a mitad de una llamada larga con B llegando tarde")
    await test_late_peer_does_not_disconnect_first()
    print()
    print("test 2: token invalido no cuelga ninguna conexion")
    await test_invalid_token_does_not_hang_either_side()
    print()
    print("TODAS LAS PRUEBAS PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
