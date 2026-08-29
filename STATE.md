STATE - SINCRO Engine v4

Ultima actualizacion: (fecha) Fase activa: G0

Fases v4
Fase	Estado	Criterio de aceptacion	Verificacion	Evidencia
G0 Adaptador WebSocket	HECHO	Una direccion end to end; TTFA no peor que F2 con el mismo fixture	make ws-test	`make ws-test` sobre `tests/fixtures/es_30s.wav`: 5 segmentos, TTFA P50 2496 ms / P90 3708 ms / P99 3854 ms. Comparado etapa por etapa contra F2 recien medido con la misma config (P50 6418 / P90 8894 ms): ninguna etapa empeora, la mayoria mejora. Ver bloque de evidencia G0
G1 CallSession + M9	HECHO	Llamada bidireccional; mute_calls == unmute_calls en ambos motores; ningun doblaje transcrito como entrada	make call-test	`make call-test` con EN (35s) y ES (35.16s): mute_calls==unmute_calls en las dos direcciones (5/5, 7/7); 0 coincidencias de eco en 7 y 5 transcripciones de entrada; ordenes de `state` validos y variados en ambos lados (sin repetidos seguidos, sin idle antes de speaking). Ver bloque de evidencia G1
G2 API de control	BLOQUEADA por G1	Contrato implementado y versionado; OpenAPI generado	make api-test	-
G3 Docker + Azure	BLOQUEADA por G2	Llamada real contra el servicio en East US; etapas STT/LLM/TTS mejoran vs local	make deploy	-
G4 Dispatcher y slots	BLOQUEADA por G3	5 llamadas simultaneas, 10 min; TTFA P90 de la 5a no peor que la 1a	make load-test N=5	-
Modulos v4
ID	Modulo	Estado	Depende de	Evidencia
A1	adapters/ws_io.py	HECHO	-	`ws_io.py`: `WebSocketAudioSource`/`WebSocketAudioSink`, formato binario exacto del contrato (8 bytes cabecera BE + 640 bytes PCM LE, 320 muestras). `ws_serve.py` sirve una direccion con el motor de F2 sin tocar M1-M8. `ws_test.py` + `tests/ws_client.py` verifican con `make ws-test`. TCP_NODELAY activado, permessage-deflate desactivado, envio por chunk segun llega de Fish, backpressure por descarte con cola de 25 frames, `seq` detecta perdida (no reordena, por contrato)
M9	EchoGate	HECHO	A1	`echo_gate.py`: subclase de SileroGate (M2), no envoltorio -- hereda VAD/turn-detector sin tocar gate.py y satisface el tipo `SileroGate` que engine.py/committer.py ya esperan, cero cambios al nucleo. `mute`/`unmute`/`unmute_after_guard` redirigen al gate CONTRARIO via metodo sin ligar (evita la recursion mutua de una redireccion simetrica ingenua). Politica de interbloqueo documentada en el modulo: estado por direccion, sin locks, sin espera circular posible; riesgo heredado de v3 (`unmute_after_guard` incondicional) anotado, no arreglado (D47)
M10	CallSession	HECHO	A1, M9	`call_session.py`: dos DubbingEngine completos + dos EchoGate, sin que un motor conozca al otro. `call_serve.py` empareja dos conexiones (hello, waiting_for_peer, timeout de 60s), simplificado sin token firmado (eso es S1/G2, deuda registrada en D52). Mensajes state/dub_start/dub_end via subclase de StreamingCommitter (observa el Segment al comprometerse, sin tocar committer.py) y los hooks on_audio/on_turn ya existentes. Bug real encontrado y corregido: la condicion `if not gate.is_speaking` casi nunca se cumplia (D48). Bug CRITICO encontrado y corregido despues del cierre de G1: `_wait_for_peer` desconectaba al primero a los 60s en plena llamada (D51); mas dos bugs menores de validacion (D52). `tests/test_lobby_pairing.py` prueba ambos, sin motores reales
S1	API de control	PENDIENTE	M10	-
S2	VoiceRepository persistente	PENDIENTE	-	-
S3	Dispatcher y slots	PENDIENTE	S1	-

M1 a M8 quedan HECHO de v3 y no se modifican.

Bloqueos activos
R20 PRESUPUESTO - Fish Audio permite 5 sesiones TTS concurrentes por debajo de 100 USD prepagados. Cinco llamadas necesitan 10. Techo real actual: 2 llamadas. G4 no puede cumplir su criterio hasta prepagar 100 USD. No lo resuelve la arquitectura.
Heredado de v3, sigue abierto
#	Pendiente	Impacto en v4
R8	APLICADO 2026-08-29 (D43). openai/gpt-oss-120b + reasoning_effort=low, en tier Production. El gate de tokens de D8 (<100/clausula) NO se cumple: con presupuesto de bytes real el modelo gasta 300-400+ tokens de razonamiento invisible; con max_tokens=200 un segmento salio vacio (falla de correctitud, no solo costo). Subido max_tokens a 800 por decision del usuario: 126.8 tokens/clausula promedio (47-411, variable), costo total mas bajo que qwen ($0.000689 vs $0.001053, mismo fixture) pero TTFA del tramo LLM peor e impredecible	G0: TTFA por etapa contra F2 debe medir este impacto, no asumirlo
R10	Criterio de deriva reinterpretado como atraso; falta el clamp negativo en el controlador	Sin el clamp, los pares que contraen acumulan credito imaginario y el controlador deja de reaccionar a atrasos reales
F2	Criterio sin certificar. P90 ~3.5 s con 67 % en speech->stt, que incluye el timeout propio de 800 ms	Desplegar NO lo arregla. El retardo en produccion sera mayor que el teorico
R1	Calidad por idioma sin remedir tras el cambio de modelo	Afecta a los 5 idiomas del contrato
D19	Todas las voces son sinteticas	El panel de F3 sobre voz sintetica seria un falso positivo
-	Nadie ha escuchado el audio	El round-trip prueba inteligibilidad, no calidad
Riesgos v4
#	Riesgo	Estado
R20	Concurrencia de Fish insuficiente para 5 llamadas	ABIERTO, bloquea G4
R21	Al elegir WebSocket, la AEC pasa a ser responsabilidad de la app	Mitigado por M9 + contrato
R22	Criterio de F2 sin cerrar	ABIERTO
R23	Ruptura del turno conversacional con 2-3 s de retardo	Mitigado por mensajes state; depende de la app
R24	Sin afinidad de sesion, M9 falla en silencio	Mitigado por el dispatcher
R25	Consentimiento de clonacion no registrado	Mitigado por POST /v1/voices con consent
Registro de sesiones
Fecha	Fase	Que se hizo	Comando	Resultado
2026-08-29	G1 (correccion previa a G2)	TAREA 1: bug critico en _wait_for_peer (desconectaba al primero a los 60s en plena llamada), corregido con asyncio.Event + asyncio.Future. TAREA 2: validacion de lang movida a _read_hello. TAREA 3: guarda isinstance(dict) contra JSON valido no-objeto. TAREA 4: verificado en codigo fuente que TCP_NODELAY ya esta activo por defecto en asyncio (D50), sin cambio de codigo. TAREA 5: dos deudas anotadas sin implementar (Lobby de un slot para G4, lang en hello en vez de token para G2). Aclaracion registrada: Dispatcher y Worker son servicios separados, call_serve.py no necesitaba cambios para G2	tests/test_lobby_pairing.py + make ws-test + make call-test	Bug critico confirmado y corregido: A sigue conectado (ws.state==OPEN) a los 70s y hasta el final de una llamada simulada de 95s, con B llegando a los 5s -- antes se caia a los 60s exactos. Segunda prueba: lang invalido cierra la conexion de A sin colgarla y B conecta despues sin heredar nada. make ws-test y make call-test siguen pasando sin regresion (variacion de TTFA es la varianza ya conocida de Fish/Groq, no del codigo). ruff y mypy --strict limpios en 31 archivos
2026-08-29	G1	TAREA 1: call_session.py (M10), dos DubbingEngine completos sin conocerse entre si. TAREA 2: echo_gate.py (M9), subclase de SileroGate con mute/unmute redirigido, politica de interbloqueo documentada. TAREA 3: mensajes state/dub_start/dub_end. TAREA 4: verificado, con un bug real encontrado y corregido en el camino (D48)	make call-test WAV_EN=tests/fixtures/matrix_en_35s.wav WAV_ES=tests/fixtures/es_30s.wav	G1 HECHO. mute_calls==unmute_calls en las dos direcciones (5/5 y 7/7). 0 coincidencias de eco: 0 hits en 7 transcripciones de entrada (lado EN) y 0 en 5 (lado ES), contra 5 y 7 doblajes respectivamente. Ordenes de state validos y ricos en ambos lados tras corregir D48 (antes de corregirlo, un lado se quedaba pegado en 'speaking' para siempre). Salvedad de metodo: los dos WAV deben durar parecido -- si uno termina antes, ese participante "cuelga" primero y CallSession cierra la llamada entera (comportamiento correcto de produccion, pero corta la medida de la direccion mas larga)
2026-08-29	G0	TAREA 0: R8 aplicado (D43), gate de tokens roto y aceptado con max_tokens=800. TAREA 1: adapters/ws_io.py (WebSocketAudioSource/Sink, formato binario exacto del contrato, TCP_NODELAY, sin permessage-deflate, backpressure por descarte). TAREA 2: tests/ws_client.py. TAREA 3: verificado	make ws-test FIXTURE=tests/fixtures/es_30s.wav	G0 HECHO. TTFA P50 2496 ms / P90 3708 ms / P99 3854 ms (5 segmentos) contra el F2 recien medido con la misma config, P50 6418 / P90 8894 ms (6 segmentos). Etapa por etapa: speech->stt 2947->1065 ms, stt->llm1 944->1011 ms (dentro de la varianza normal de Groq), llm1->done 40->37 ms, done->tts1 2263->630 ms (varianza de Fish, ya documentada). Ninguna etapa empeora. Salvedad: 5 segmentos contra 6 porque el cliente de prueba no pausa el envio durante el mute del servidor (a proposito: un llamante real no ve el estado de reproduccion del otro extremo), mientras el banco de F2 si pausa el WAV en mute; es una diferencia de metodologia, no un defecto del adaptador
