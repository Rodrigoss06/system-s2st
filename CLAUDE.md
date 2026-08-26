# SINCRO Engine - Motor de doblaje en tiempo real

Motor de doblaje voz a voz con clonacion de timbre. Alcance v3: nucleo aislado,
demo por consola, un hablante, idioma declarado. Sin sala, sin navegador, sin backend.

## Fuentes de verdad

1. `STATE.md` - progreso real con evidencia. **LEELO ANTES DE CUALQUIER TAREA.**
2. `DECISIONS.md` - desvios del plan, con fecha y razon.
3. Notion, *SINCRO Motor v3 - Documentacion tecnica* - contratos, arquitectura,
   politicas de commit, isocronia, telemetria.
4. Notion, *SINCRO Motor v3 - Proyecto* - fases, criterios de aceptacion, riesgos.

Si este archivo y Notion se contradicen, gana Notion y hay que corregir aqui.

## Protocolo obligatorio por tarea

**Antes de escribir codigo:**

1. Lee `STATE.md`. Identifica la fase activa.
2. Verifica que los modulos de los que depende tu tarea esten marcados HECHO con evidencia.
3. Si un modulo del que dependes NO esta hecho: **detente y dilo**. No crees un stub
   silencioso ni asumas su comportamiento. Un stub sin registrar es la causa numero uno
   de que el sistema deje de avanzar.
4. Si la tarea pertenece a una fase posterior a la activa: **detente y dilo**.

**Despues de escribir codigo:**

1. Ejecuta el comando de verificacion de la tarea.
2. Actualiza `STATE.md` con: que se hizo, el comando ejecutado y la salida relevante.
3. Sin salida ejecutada, el criterio NO se marca como cumplido. "Deberia funcionar"
   no es evidencia.
4. Si desviaste del contrato o del stack, anotalo en `DECISIONS.md`.

## Arquitectura - reglas no negociables

- **El motor es agnostico del transporte.** Ningun archivo bajo `src/sincro/` excepto
  `adapters/` puede importar `sounddevice` ni `livekit.rtc`. Si necesitas audio I/O
  dentro de un modulo, el contrato esta mal.
- **No usar `AgentSession` ni `Room` de livekit-agents.** Su semantica es conversacional
  (usuario habla, agente responde); nosotros hacemos passthrough. Usar los plugins en
  modo standalone: `STT.stream()`, `LLM.chat()`, `TTS.stream()`.
- **Un modulo, una responsabilidad.** Los ocho modulos estan definidos en Notion.
  No fusionar dos porque resulte comodo.
- **Cada Protocol tiene un `Fake` determinista** en `fakes.py`. Si agregas un Protocol,
  agregas su Fake en el mismo commit.
- **Nunca traducir transcripciones parciales.** Solo `is_final`.
- **Todo evento pasa por `telemetry.py`.** Un camino de codigo sin telemetria es un
  camino que no se puede verificar.

## Stack - no sustituir sin registrar en DECISIONS.md

| Capa | Componente |
|---|---|
| VAD | Silero, `livekit-plugins-silero` |
| Endpointing | `livekit-plugins-turn-detector`, MultilingualModel |
| STT | Deepgram Nova-3 Monolingual, streaming, idioma fijo |
| Traduccion | Groq `llama-3.3-70b-versatile` |
| TTS | Fish Audio `s2.1-pro`, en dev `s2.1-pro-free` |
| Audio I/O | `sounddevice` |

Idiomas soportados: `es`, `en`, `pt-BR`, `fr`, `ja`. Declarados por variable de entorno.
No implementar autodeteccion ni `language=multi`.

## Restricciones de coste y latencia

- Groq **no tiene prompt caching** en Llama 3.3 70B. El system prompt se paga entero en
  cada clausula. Mantenlo bajo 200 tokens. Contexto rodante: maximo 3 turnos.
- Objetivo TTFA P90: menos de 2.0 s en F2.
- `speed` de TTS acotado a 0.95 - 1.25. Fuera de ese rango el timbre clonado se degrada.
- Durante desarrollo usar `s2.1-pro-free` para que el TTS cueste cero.

## Anti-eco - critico en consola

Microfono y altavoz en la misma maquina se realimentan: el motor transcribe su propia
salida y entra en bucle. Dos defensas, ambas obligatorias:

1. Auriculares. Avisarlo al arrancar `make live`.
2. Puerta dura: `AudioGate.mute()` mientras `Synthesizer` reproduce, `unmute()` al
   terminar mas 150 ms de guarda.

Nunca desactivar la segunda "porque tengo auriculares".

## Comandos

| Comando | Que hace |
|---|---|
| `make check` | Valida las 3 credenciales y emite un JSONL de prueba |
| `make dub-file IN=x.wav` | Cascada offline sobre archivo, F1 |
| `make live` | Microfono a altavoz, F2 |
| `make enroll REF=voz.wav` | Registra timbre, devuelve reference_id, F3 |
| `make drift-test` | WAV de 10 min, mide deriva acumulada, F4 |
| `make matrix-test` | Los 20 pares dirigidos, F5 |
| `make soak MIN=20` | Sesion larga con corte de red simulado, F6 |
| `make report` | Agrega el JSONL: P50 P90 P99 de TTFA, triggers, deriva, coste |

## Estilo

- Python 3.11+, tipado estricto, `async` en todo el pipeline.
- Sin comentarios que expliquen lo obvio. Comentar solo el porque, nunca el que.
- Errores de red: reintento con backoff, jamas fallo silencioso.
- Logs en ingles, documentacion y commits en espanol.
