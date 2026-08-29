SINCRO Engine v4 - Servicio de doblaje en tiempo real

El motor de v3 desplegado como servicio multi-sesion en Azure, con WebSocket crudo. Objetivo: 5 llamadas simultaneas, dos direcciones cada una. La app movil es un proyecto aparte; este repositorio expone el contrato que ella consume.

Fuentes de verdad
STATE.md - progreso real con evidencia. LEELO ANTES DE CUALQUIER TAREA.
DECISIONS.md - desvios del plan, con fecha y razon.
Notion, SINCRO Motor v4 - Contrato de conexion - EL CONTRATO ES PUBLICO. Otro equipo construye contra el. Cambiarlo de forma incompatible rompe su trabajo.
Notion, SINCRO Motor v4 - Despliegue en Azure - topologia, workers, riesgos, fases.
Notion, SINCRO Motor v3 - Documentacion tecnica - los contratos del nucleo siguen vigentes sin cambios.
Lo que NO cambia de v3

El nucleo no se toca. M1 a M8 quedan exactamente como estan. v4 solo anade adaptadores, un coordinador de llamada y la capa de servicio.

Si te encuentras editando gate.py, transcriber.py, committer.py, translator.py, voices.py, synthesizer.py, drift.py o telemetry.py: PARA. Casi seguro el cambio corresponde a un adaptador.

Reglas de arquitectura - no negociables
EL NUCLEO ES AGNOSTICO DEL TRANSPORTE. Ningun archivo bajo src/sincro/ excepto adapters/ puede importar websockets, fastapi, aiohttp ni sounddevice. Esta regla importa mas en v4 que en v3: es lo unico que hace que el motor sobreviva al siguiente cambio de transporte.
DOS MOTORES POR LLAMADA. Cada direccion es una instancia completa de DubbingEngine. El contexto rodante, el reloj de deriva, el reference_id y el contador de segmentos son estado POR HABLANTE. Compartirlos los contamina cruzado.
CallSession posee los dos motores y M9. Ningun motor conoce al otro directamente.
EL CONTRATO ES VERSIONADO. Cambio incompatible = v1 pasa a v2. Nunca se modifica v1 en sitio.
Cada Protocol nuevo trae su Fake determinista en el mismo commit.
Todo evento pasa por telemetry.py.
M9 EchoGate - por que existe

En v3 la compuerta anti-eco vivia dentro del motor: el sintetizador silenciaba su propia entrada. En v4 eso NO basta, y la razon es un cruce facil de pasar por alto:

El altavoz de A reproduce el doblaje del motor B->A.
El microfono de A alimenta al motor A->B.

Son motores distintos. La compuerta interna de cada uno no ve al otro. Si el dispositivo filtra altavoz a microfono, A->B recibe habla inglesa doblada, la reconoce sin problema porque su STT esta fijado en ingles, la traduce y la reenvia. Bucle de realimentacion con contenido plausible, mucho peor de diagnosticar que ruido.

M9 vive en CallSession: mientras B->A emita hacia el altavoz de A, silencia la entrada de A->B. Guarda de 150 ms, igual que v3.

Es defensa parcial: el gate es de servidor y el eco ocurre en el dispositivo. La segunda defensa es obligacion de la app y esta en el contrato.

Reglas del camino de audio - optimizacion

El TTFA ya esta en el limite. Cada una de estas reglas evita anadirle latencia:

TCP_NODELAY activado en el socket. Con frames de 640 bytes cada 20 ms, el algoritmo de Nagle los coalesce y anade decenas de ms invisibles.
permessage-deflate DESACTIVADO. El PCM no comprime bien y la compresion cuesta CPU y latencia por frame.
NO reagrupar frames entrantes. Los 20 ms llegan y van directo al VAD. Cualquier acumulacion se suma a speech->stt, que ya es el 67 % del TTFA.
REENVIAR EL AUDIO DEL TTS SEGUN LLEGA. Fish emite en streaming; el WebSocket reenvia cada chunk al recibirlo. Esperar a la sintesis completa anade la duracion entera del audio al retardo percibido. Este es el error mas caro posible.
Tareas de envio y recepcion SEPARADAS por conexion. Un envio lento no puede bloquear la recepcion del microfono.
Backpressure por descarte, nunca por cola: si el cliente no da abasto, descarta frames antiguos. El audio en tiempo real caducado no vale nada y la cola solo empeora el retardo.
Registrar el descarte en telemetria. Un descarte silencioso es un bug invisible.
Afinidad de sesion

Los dos participantes de una llamada DEBEN aterrizar en el mismo worker, porque M9 coordina motores que viven en el mismo proceso.

Por eso el dispatcher ASIGNA worker, no balancea: devuelve una URL con el FQDN del worker concreto. Si los dos usuarios caen en replicas distintas, el anti-eco falla EN SILENCIO, sin error visible. Nunca pongas un balanceador round-robin delante del endpoint de stream.

Stack
Capa	Componente
VAD	Silero, local
Endpointing	turn-detector multilingue, runner local (D14)
STT	Deepgram Nova-3 Monolingual, SDK oficial, WebSocket
Traduccion	Groq openai/gpt-oss-120b con reasoning_effort=low
TTS	Fish Audio s2.1-pro
Transporte	WebSocket crudo sobre TLS, puerto 8080
Runtime	Azure Container Apps, region East US

Region East US no es preferencia: Deepgram, Groq y Fish estan en EE.UU. Desplegar mas cerca de Peru mete RTT transcontinental en tres saltos secuenciales del camino critico.

Techos que no se negocian con arquitectura
Fish Audio: 5 requests concurrentes por debajo de 100 USD prepagados, 15 desde 100. Una llamada consume 2. El objetivo de 5 llamadas NECESITA los 100 USD; sin ellos el techo real son 2 llamadas.
Groq: limites de RPM y TPM por tier. Una llamada genera ~38 requests/min.
Comandos
Comando	Que hace
make check	Credenciales y modelos cargables
make ws-test	Una direccion end to end contra cliente de prueba, G0
make call-test	Llamada bidireccional simulada con dos clientes, G1
make api-test	Contrato del documento de conexion, G2
make docker-build	Imagen con pesos incluidos
make deploy	Container Apps East US, G3
make load-test N=5	N llamadas simultaneas sostenidas, G4
make report	P50 P90 P99 de TTFA, triggers, deriva, descartes, coste
Estilo
Python 3.13, tipado estricto, async en todo el camino de audio.
Errores de red: reintento con backoff, jamas fallo silencioso.
Una sesion no se cae entera por el fallo de una etapa. Politica de F6 elevada a nivel de llamada.
Logs en ingles, documentacion y commits en espanol.
