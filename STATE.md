STATE - SINCRO Engine v4

Ultima actualizacion: (fecha) Fase activa: G0

Fases v4
Fase	Estado	Criterio de aceptacion	Verificacion	Evidencia
G0 Adaptador WebSocket	PENDIENTE	Una direccion end to end; TTFA no peor que F2 con el mismo fixture	make ws-test	-
G1 CallSession + M9	BLOQUEADA por G0	Llamada bidireccional; mute_calls == unmute_calls en ambos motores; ningun doblaje transcrito como entrada	make call-test	-
G2 API de control	BLOQUEADA por G1	Contrato implementado y versionado; OpenAPI generado	make api-test	-
G3 Docker + Azure	BLOQUEADA por G2	Llamada real contra el servicio en East US; etapas STT/LLM/TTS mejoran vs local	make deploy	-
G4 Dispatcher y slots	BLOQUEADA por G3	5 llamadas simultaneas, 10 min; TTFA P90 de la 5a no peor que la 1a	make load-test N=5	-
Modulos v4
ID	Modulo	Estado	Depende de	Evidencia
A1	adapters/ws_io.py	PENDIENTE	-	-
M9	EchoGate	PENDIENTE	A1	-
M10	CallSession	PENDIENTE	A1, M9	-
S1	API de control	PENDIENTE	M10	-
S2	VoiceRepository persistente	PENDIENTE	-	-
S3	Dispatcher y slots	PENDIENTE	S1	-

M1 a M8 quedan HECHO de v3 y no se modifican.

Bloqueos activos
R20 PRESUPUESTO - Fish Audio permite 5 sesiones TTS concurrentes por debajo de 100 USD prepagados. Cinco llamadas necesitan 10. Techo real actual: 2 llamadas. G4 no puede cumplir su criterio hasta prepagar 100 USD. No lo resuelve la arquitectura.
Heredado de v3, sigue abierto
#	Pendiente	Impacto en v4
R8	qwen3.6-27b es tier Preview. gpt-oss-120b low esta en Production, pasa el gate de tokens y cuesta menos	Se aplica en G0. No se despliega infraestructura sobre un modelo marcado como no apto para produccion
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
