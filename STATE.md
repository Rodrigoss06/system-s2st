# STATE - SINCRO Engine v3

Ultima actualizacion: 2026-08-26
Fase activa: F6

## Fases

| Fase | Estado | Criterio | Evidencia |
|---|---|---|---|
| F0 Esqueleto | HECHO | make check sale 0 | `make check` -> `RESULT: PASS - 3/3 credentials valid, telemetry JSONL emitted.` `EXIT=0`. Ver bloque de evidencia F0 abajo |
| F1 Cascada offline | HECHO | 6 segmentos de un WAV de 30 s | `make dub-file IN=tests/fixtures/es_30s.wav` -> 6 segmentos, 6 traducciones correctas, WAV de 22.80 s. Round-trip STT del doblaje: confianza 0.999. Revalidado el 2026-08-26 con `qwen/qwen3.6-27b`. Ver bloque de evidencia F1 |
| F2 Streaming en vivo | HECHO (*) | TTFA P90 menor a 2.0 s en 20 turnos | Pipeline completo y funcionando extremo a extremo. **Criterio NO certificado**: exige voz real por microfono, 20 turnos, 5 min. Sobre el banco `--from-wav`: TTFA P90 3301-4783 ms segun ajuste. **(*) Cerrada por decision del usuario el 2026-08-26 SIN la medida por microfono (D18).** Ver bloque de evidencia F2 |
| F3 Clonacion | HECHO (*) | 3 de 3 identifican el timbre | Mecanismo completo y verificado: enrolamiento HTTP 201 `state=trained`, contraste A/B medido (dF0 5.7 Hz clonada vs 63.3 Hz neutra). **(*) Cerrada por decision del usuario el 2026-08-26; el panel de 3 personas sigue pendiente**, material listo en `out/ab/`. Ver bloque de evidencia F3 |
| F4 Isocronia y deriva | HECHO (*) | deriva menor a 300 ms por 10 min | **NO CUMPLE** con la lectura literal: -82.61 s sobre 10 min. Es aritmetica, no un bug: el minimo posible con el piso de speed en 0.95 es -73.5 s (D23). **Atraso maximo +0.00 s**: el doblaje nunca va por detras. `speed` en rango en el 100 % de los segmentos. **(*) Cerrada por decision del usuario el 2026-08-26 con el criterio literal sin cumplir (R10).** Ver bloque de evidencia F4 |
| F5 Cinco idiomas | HECHO (*) | 20 pares inteligibles | Los 20 pares producen audio inteligible: RT confianza 0.997-1.000 en todos. 12 factores recalibrados y aplicados. **R1 sin concluir**: dos bugs de japones corregidos (D28, D29) no se pudieron remedir, cuota diaria de Groq agotada (D30). **(*) Cerrada por decision del usuario el 2026-08-26 con R1 sin concluir.** Ver bloque de evidencia F5 |
| F6 Endurecimiento | HECHO | 20 min sin intervencion | `make soak`: 20.0 min, 195 turnos, ids 1..195 **sin huecos**, 1 reconexion, 4.9 s sin socket, 226 frames reenviados, 0 perdidos. Los 4 puntos del criterio en SI. Ver bloque de evidencia F6 |

## Modulos

| ID | Modulo | Estado | Depende de | Evidencia |
|---|---|---|---|---|
| M1 | LanguageProfile | HECHO | - | `config.py`. Los 20 pares dirigidos resuelven codigo Deepgram, codigo turn-detector y factor de expansion. Los 4 pares con `ja` devuelven `0.0` = sin presupuesto de bytes. Factores provisionales: **F5 los recalibra por duracion medida** |
| M2 | AudioGate | HECHO | M1 | `gate.py`: Silero VAD con prefijo y hangover, puerta dura `mute`/`unmute` con guarda de 150 ms, y turn-detector multilingue local (D14). Anti-eco verificado: `mute_calls == unmute_calls`, `dropped_muted=0` con hablante que pausa |
| M3 | Transcriber | HECHO | M1, M2 | `transcriber.py`: dos caminos. Pre-grabado REST (F1) y **WebSocket con el SDK oficial de Deepgram (F2, D13)**, con parciales, finales, `speech_final` y `words[]` con confianza. Mapea la linea temporal del stream a tiempo de captura. **Reconexion con backoff pendiente: es F6 (R5)** |
| M4 | SegmentCommitter | HECHO | M3 | `committer.py`: `PunctuationCommitter` (F1) y `StreamingCommitter` (F2) con **los 4 triggers** y guardas de no-corte por idioma. `is_final` es la compuerta; `speech_final` corrobora, nunca dispara solo (D16). Une palabras sin espacio en japones (D28). Trigger registrado en telemetria |
| M5 | Translator | HECHO | M1, M4 | `translator.py`: Groq via plugin de OpenAI con `qwen/qwen3.6-27b` y `reasoning_effort=none`, `prompts/translate.md`, contexto rodante de 3 turnos, token `[[SKIP]]`, deteccion de marcadores de control, presupuesto de bytes. 16.2 tokens de salida por clausula. Ver D8 y D12 |
| M6 | VoiceRegistry | HECHO | - | `voices.py`: `FishVoiceRegistry` con `enroll()`, `get()`, `remember()` y `delete()`. Crea el modelo por REST (D20), cache solo en memoria, clip nunca a disco, `visibility=private`. Consentimiento explicito antes de leer el clip |
| M7 | Synthesizer | HECHO | M5, M6 | `synthesizer.py`: `synthesize()` (F1) y `synthesize_stream()` por WebSocket con `latency_mode=balanced` (F2). Pasa el `reference_id` como `voice_id` al plugin (F3). `speed` acotado 0.95-1.25, sesion HTTP propia (D9) |
| M8 | Drift y Telemetry | HECHO | M2 a M7 | `telemetry.py` con las 6 marcas y `report.py` agregando el JSONL. `drift.py`: `DriftController` con `THRESHOLD_SOFT`, `THRESHOLD_HARD` y `RESET_SILENCE` tal como el documento, mas curva ASCII. Conectado a la cascada offline y al motor en vivo. **Su criterio no se cumple con la lectura literal: ver D23** |

Estados validos: PENDIENTE, EN CURSO, HECHO, BLOQUEADA.
HECHO exige comando ejecutado y salida pegada en la columna Evidencia.

## Evidencia F0

### Entregado

```
pyproject.toml            Python >=3.11,<3.14, dependencias de la seccion 10 + httpx (D3)
Makefile                  12 targets; los 7 de fases futuras fallan con mensaje explicito
.env.example              3 credenciales + 5 variables de configuracion
.gitignore                .env y out/ excluidos
DECISIONS.md              D1 a D6
src/sincro/contracts.py   6 dataclasses + 6 Protocols, tipados, sin logica
src/sincro/config.py      M1: tabla de 5 idiomas, 20 pares, load_settings()
src/sincro/telemetry.py   M8: SegmentRecord + TelemetryWriter, esquema exacto
src/sincro/fakes.py       1 Fake determinista por Protocol, ninguno toca la red
src/sincro/check.py       healthcheck de credenciales + cascada de fakes
src/sincro/adapters/      vacio, se llena en F1
```

No se implemento ningun modulo real. `gate.py`, `transcriber.py`, `committer.py`,
`translator.py`, `voices.py`, `synthesizer.py`, `drift.py` y `engine.py` **no existen**:
no hay stubs silenciosos.

### `make setup`

```
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
$ .venv/bin/python --version
Python 3.13.14
```

El interprete del sistema es 3.14.5, fuera del rango soportado. Ver D2.

### `make check` - criterio de aceptacion de F0

```
$ make check
.venv/bin/python -m sincro.check
SINCRO Engine v3 - make check (F0)
  pair       : es -> en
  tts model  : s2.1-pro-free
  expansion  : 0.85

[1/2] credentials
  PASS  deepgram  HTTP 200
  PASS  groq      HTTP 200
  PASS  fish      HTTP 200
  (0.76s)

[2/2] fake pipeline telemetry
  PASS  6 segments, 6/6 stage marks each
  PASS  jsonl -> out/telemetry-20260825T205342.jsonl
        triggers=eou  cost_usd=0.00077838

RESULT: PASS - 3/3 credentials valid, telemetry JSONL emitted.
EXIT=0
```

### JSONL emitido - primera linea

```json
{"seg_id": 1, "lang_src": "es", "lang_dst": "en", "trigger": "eou",
 "t_speech_end": 44903.187, "t_stt_final": 44903.187, "t_llm_first_token": 44903.187,
 "t_llm_done": 44903.187, "t_tts_first_byte": 44903.187, "t_audio_out": 44903.188,
 "ttfa_ms": 1, "source_duration_s": 3.0, "audio_duration_s": 1.333, "speed_applied": 1.0,
 "drift_s": -1.667, "bytes_in": 20, "bytes_out": 20, "byte_budget": 17,
 "tokens_in": 205, "tokens_out": 5, "cost_usd": 0.0001249}
```

Comparacion contra el esquema de la seccion 8 del documento tecnico:

```
orden y claves identicos al documento: True
faltantes: set()  sobrantes: set()
```

Las 6 marcas de etapa (`t_speech_end`, `t_stt_final`, `t_llm_first_token`, `t_llm_done`,
`t_tts_first_byte`, `t_audio_out`) estan presentes en los 6 segmentos. El `ttfa_ms` de 1 ms
es correcto y esperado: los fakes no tocan la red. Aqui se verifica el **camino**, no la
latencia; la latencia se mide en F2.

### `make lint`

```
$ make lint
.venv/bin/ruff check src/
All checks passed!
.venv/bin/mypy
Success: no issues found in 7 source files
```

mypy corre en modo `strict`.

### Determinismo de los fakes

```
reference_id  : fake-ec2e925367d1fe15  (estable: True)
digest cascada: bd11e50967e418e1b08e80800a4ffb8f
identico en 2 corridas: True

'Hola mundo' -> 'odnum aloH'
len= 3 -> 0.200s
len=10 -> 0.667s
len=30 -> 2.000s
```

### Targets de fases futuras

Los 7 fallan antes de importar nada, nombrando su fase:

```
$ make live
ERROR: 'live' pertenece a F2 Streaming en vivo y todavia no esta implementado.
       Fase activa segun STATE.md: F0
       Cierra F2 Streaming en vivo con evidencia en STATE.md antes de usar este target.
make: *** [Makefile:59: live] Error 1
```

Igual para `dub-file` (F1), `enroll` (F3), `drift-test` (F4), `matrix-test` (F5),
`soak` (F6) y `report` (F1). `dub-file` sin `IN` y `enroll` sin `REF` fallan antes,
pidiendo el argumento.

## Evidencia F1

Revalidada el 2026-08-26 tras el cambio de modelo (D8). La evidencia del 2026-08-25, que
corrio con `openai/gpt-oss-120b`, queda sustituida por esta.

### Entregado

```
src/sincro/adapters/file_io.py   lee WAV mono 16 kHz como SpeechFrame, escribe el doblaje
src/sincro/transcriber.py        M3  Deepgram Nova-3 pre-grabado
src/sincro/committer.py          M4  corte por puntuacion sobre words[]
src/sincro/translator.py         M5  Groq + contexto rodante 3 turnos + [[SKIP]]
src/sincro/synthesizer.py        M7  Fish s2.1-pro-free, voz por defecto
src/sincro/dub_file.py               orquestacion secuencial de F1
src/sincro/report.py                 agrega el JSONL
tests/make_fixture.py                genera los WAV de referencia
tests/fixtures/es_30s.wav            34.70 s, 16 kHz mono, 6 clausulas
```

No existen `gate.py`, `voices.py`, `drift.py` ni `engine.py`: son F2, F3, F4 y F2.

### `make dub-file IN=tests/fixtures/es_30s.wav`

```
input      : tests/fixtures/es_30s.wav  34.70s  16000 Hz  1ch
pair       : es -> en
llm        : qwen/qwen3.6-27b  reasoning_effort=none  temp=0.2  max_tokens=200
tts        : s2.1-pro-free  (voz por defecto; la clonacion es F3)
expansion  : 0.85

seg  1 [punctuation]   0.08-  4.32s
   src: Buenos días a todos, y gracias por conectarse a la reunión de esta mañana.
   dst: Good morning, everyone, and thanks for joining this morning's meeting.
   bytes 77 -> 70 (budget 65)  audio 3.11s vs 4.24s  drift -1.13s

seg  2 [punctuation]   5.04- 10.96s
   src: Hoy vamos a revisar el informe del 3º trimestre y los números de la región andina.
   dst: Today we will review the Q3 report and the Andean region figures.
   bytes 85 -> 65 (budget 72)  audio 3.95s vs 5.92s  drift -3.10s

seg  3 [punctuation]  11.52- 16.65s
   src: Las ventas subieron un 12 por 100 respecto al mismo período del año pasado.
   dst: Sales rose 12% vs. the same period last year.
   bytes 77 -> 45 (budget 65)  audio 4.04s vs 5.13s  drift -4.20s

seg  4 [punctuation]  17.14- 22.09s
   src: El equipo de Lima cerró 3 contratos nuevos y el de Arequipa cerró 2.
   dst: The Lima team closed 3 new contracts and Arequipa 2.
   bytes 70 -> 52 (budget 59)  audio 4.37s vs 4.96s  drift -4.79s

seg  5 [punctuation]  23.27- 29.67s
   src: Necesitamos el presupuesto aprobado antes del 15 de marzo de 2027.
   dst: We need the approved budget before March 15, 2027.
   bytes 66 -> 50 (budget 56)  audio 3.90s vs 6.40s  drift -7.29s

seg  6 [punctuation]  30.31- 33.83s
   src: Si no hay preguntas, cerramos aquí y seguimos por correo electrónico.
   dst: If there are no questions, we'll close here and follow up by email.
   bytes 71 -> 67 (budget 60)  audio 3.44s vs 3.52s  drift -7.37s

segments   : 6
skipped    : 0
leaked     : 0 segments with control markers
partials   : 0 discarded (is_final gate)
dubbed wav : out/dubbed-20260826T014457.wav  22.80s  1005568 samples
telemetry  : out/telemetry-20260826T014457.jsonl
source     : 34.70s   dubbed: 22.80s   delta: -11.89s
```

**Criterio cumplido, verificado punto por punto:**

| Comprobacion | Resultado |
|---|---|
| Exactamente 6 segmentos | Si. 6 frases -> 6 segmentos |
| Las 6 traducciones son correctas | Si. Ninguna falla |
| Numeros, fechas y nombres propios preservados | Si en las 6 |
| Sin preambulo, comillas, explicacion ni tokens de control | Si. `leaked: 0` |

Invariantes preservados: `3º trimestre` -> `Q3`, `12 por 100` -> `12%`, `Lima`,
`Arequipa`, `3` y `2`, `15 de marzo de 2027` -> `March 15, 2027`.

`translator.py` comprueba activamente la fuga de marcadores (`<think>`, `<|return|>`,
`<|channel|>`, `analysisfinal`) y la contabiliza en `leaked`. Salio 0.

Nota de calidad: qwen3.6-27b es un modelo de 27B frente a los 120B anteriores, pero en
este fixture **no se observa degradacion**. En seg 1 mejora: gpt-oss-120b tradujo
`de esta mañana` como `morning's meeting`, perdiendo el demostrativo; qwen3.6 devuelve
`this morning's meeting`. Es una muestra de 6 clausulas en un solo par de idiomas: no
autoriza a concluir nada general. F5 mide los 20 pares.

### GATE DE TOKENS - criterio de aprobacion menos de 100 por clausula

```
 seg  tokens_in  tokens_out
   1        183          15
   2        196          16
   3        212          15
   4        227          16
   5        228          18
   6        224          17

  total salida : 97
  por clausula : 16.2  (max 18, min 15)
  RESULTADO    : VERDE

  antes (gpt-oss-120b, reasoning medium): 3504 total = 584.0 por clausula
  ahora (qwen3.6-27b, reasoning none)   :   97 total =  16.2 por clausula
  reduccion: 36.1x
```

**VERDE.** 16.2 por clausula, por debajo del limite de 100 y tambien por debajo de los 28
que estimaba el presupuesto original del proyecto. El maximo por clausula es 18, asi que
no hay una cola larga escondida detras de la media.

Esto desbloquea F2: los 0.6-1.2 s de latencia de razonamiento por clausula desaparecen
del presupuesto de TTFA.

### `make report`

```
SINCRO report - out/telemetry-20260826T014457.jsonl
6 segmentos   es -> en

TTFA
  P50     13978 ms
  P90     18304 ms
  P99     19233 ms
  min      6590 ms    max 19336 ms

Triggers
  !! eou            0    0.0%   objetivo 55%-70%
  !! punctuation    6  100.0%   objetivo 20%-35%
  ok timeout        0    0.0%   objetivo 0%-10%
  ok max_len        0    0.0%   objetivo 0%-3%
     nota: sin eou -> turn-detector inactivo (F2). La distribucion
           objetivo se evalua a partir de F2.

Isocronia y deriva
  audio fuente       30.18 s
  audio doblado      22.80 s
  deriva final       -7.37 s
  deriva max         -7.37 s
  speed  min/max  1.000 / 1.000
  bytes  in/out   446 / 349   ratio 0.783

Coste
  tokens in/out   1270 / 97
  total           $0.001053
  por minuto      $0.002094/min de habla
```

El TTFA de F1 **no es comparable con el objetivo de F2**. En offline el archivo se
transcribe de una vez, asi que `t_speech_end` es comun a los seis segmentos y estos se
procesan en serie: el P90 mide la cascada entera acumulada, no la latencia por turno.
F2 lo mide en vivo, que es donde el criterio de 2.0 s aplica.

`eou` a 0 % es correcto: necesita el turn-detector, que es F2. El reporte lo anota.

La deriva de -7.37 s es negativa, es decir el doblaje es **mas corto** que la fuente, y no
se corrige: `drift.py` es F4. `speed` se queda fijo en 1.000 por el mismo motivo.

**Coste.** Bajo de $0.003674 a $0.001053 en la misma corrida, y de $0.007247 a
$0.002094 por minuto de habla. Las tarifas ya no son las de Llama 3.3: `telemetry.py`
tiene una tabla `LLM_PRICES` por nombre de modelo, tomada del campo `pricing` del
endpoint `/models` de Groq.

### WAV doblado: verificacion objetiva

No escuche el archivo. Se midio y se paso de vuelta por Deepgram en ingles,
confianza **0.999**:

```
Good morning, everyone, and thanks for joining this morning's meeting. Today, we will
review the Q3 report and the Andean region figures. Sales rose 12% versus the same period
last year. The Lima team closed three new contracts and Araquipa two. We need the approved
budget before 03/15/2027. If there are no questions, we'll close here and follow-up by email.
```

Las seis frases se recuperan intactas con numeros y fecha preservados. Es prueba objetiva
de que el WAV contiene habla inglesa inteligible, no ruido. **No sustituye a una escucha
humana**, que sigue pendiente.

`Arequipa` vuelve como `Araquipa` (con gpt-oss-120b volvia como `Arkhipa`). Ver el riesgo
de pronunciacion de nombres propios en preguntas abiertas.

### `make lint`

```
$ make lint
.venv/bin/ruff check src/
All checks passed!
.venv/bin/mypy
Success: no issues found in 14 source files
```

## Evidencia F2 - EN CURSO, criterio no certificado

### Entregado

```
src/sincro/gate.py                 M2  Silero VAD + turn-detector local + puerta anti-eco
src/sincro/transcriber.py          M3  + DeepgramStreamTranscriber (WebSocket, SDK oficial)
src/sincro/committer.py            M4  + StreamingCommitter, 4 triggers + guardas
src/sincro/synthesizer.py          M7  + synthesize_stream(), WebSocket latency balanced
src/sincro/adapters/console_io.py      sounddevice, microfono y altavoz
src/sincro/adapters/lk_frames.py       unica frontera con livekit.rtc (D15)
src/sincro/engine.py                   DubbingEngine, dos lazos concurrentes
src/sincro/live.py                     make live
```

`make live` imprime el aviso de auriculares y pide confirmacion **antes** de abrir el
microfono. Verificado.

### Lo que SI esta verificado

Corridas con `--from-wav`, que ejercita VAD, WebSocket, los 4 triggers, traduccion, TTS
en streaming y la puerta anti-eco:

- Pipeline completo extremo a extremo, sin fallo silencioso.
- Anti-eco balanceado: `mute_calls == unmute_calls` en todas las corridas.
- Compuerta `is_final`: 29-30 parciales descartados por sesion, ninguno traducido.
- `dropped_muted = 0` con hablante que pausa; el gate cierra y abre correctamente.
- Guardas de no-corte activas (1 corte evitado en la corrida de 0.40).
- F0 y F1 **sin regresion**: `make check` sale 0, `make dub-file` sigue dando 6/6.

### Lo que NO esta verificado

**El criterio de aceptacion.** Exige voz real por microfono, 20 turnos y 5 minutos
continuados. No puedo hablar por un microfono: hace falta el usuario. Ninguna medida de
abajo lo sustituye.

### Ajustes probados y su TTFA (banco `--from-wav`, fixture de 6 clausulas)

`min_silence_duration` de Silero, que es la perilla que el criterio manda tocar primero:

| min_silence | turnos | TTFA P50 | TTFA P90 | triggers |
|---|---|---|---|---|
| 0.55 (default) | 7 | 2892 ms | 3868 ms | eou 4, timeout 3 |
| 0.40 | 6 | 2674 ms | 3958 ms | eou 4, timeout 2 |
| 0.30 | 6 | 2530 ms | 3811 ms | eou 3, punct 1, timeout 2 |
| 0.25 (minimo) | 7 | 3504 ms | 4783 ms | eou 3, punct 1, timeout 3 |

**La perilla no mueve la aguja.** El rango entero cae entre 3811 y 4783 ms de P90, sin
tendencia clara, y 0.25 empeora. El cuello de botella no es el VAD.

Otros ajustes probados:

| Ajuste | Resultado | Decision |
|---|---|---|
| Deepgram `endpointing` 300 -> 150 ms | `speech->stt` 1531 -> 1574 ms, sin mejora | Revertido a 300 |
| `timeout` dinamico 0.8 -> 2.5 s con eou bajo | P90 3668 -> 4291 ms, **peor** | Revertido (D16) |
| Semantica de `unlikely_threshold` corregida | Elimino los cortes a media frase | **Mantenido** (D16) |
| Mapeo de linea temporal Deepgram -> captura | P90 8030 -> 3592 ms | **Mantenido**, era un error de medida |

### Donde se va el tiempo, del desglose por etapas de la telemetria

```
media por etapa (7 segmentos):
  speech->stt      2296 ms    67.4%
  stt->llm1         677 ms    19.9%
  llm1->done         23 ms     0.7%
  done->tts1        410 ms    12.0%
  tts1->out           0 ms     0.0%
```

**El 67 % del TTFA esta entre que el hablante calla y que M4 cierra el segmento.** El LLM
genera en 23 ms (el gate de tokens de D8 hizo su trabajo) y el TTS responde en 410 ms.
LLM + TTS suman ~1110 ms, asi que quedan ~890 ms de presupuesto para esa primera etapa.

Los mejores turnos ya caen dentro: seg 7 con `speech->stt` de 881 ms da **TTFA 1546 ms**,
y seg 4 da 2040 ms. La arquitectura puede cumplir. Lo que rompe el P90 son los turnos
donde dispara `timeout`, que suma sus 800 ms integros.

### Por que el banco de pruebas castiga el resultado

El fixture es voz sintetica con pausas de 0.6 s entre clausulas (D10, D17). Esas pausas
hacen que Deepgram cierre fragmentos **a mitad de frase**, y entonces el trigger `timeout`
dispara donde un hablante humano no habria pausado. Se ve en la distribucion: `timeout`
sale al 29-43 % cuando el objetivo del documento es menos del 10 %.

Con voz humana real, que pausa entre turnos y no a mitad de sintagma, se espera que `eou`
absorba esos casos. **Es una expectativa, no un resultado.** Se mide con microfono.

### Siguiente paso concreto

Ejecutar `make live` con auriculares y hablar 20 turnos. El resumen de sesion ya imprime
P50/P90/P99, la distribucion de triggers y el veredicto del criterio. Si el P90 real sigue
por encima de 2.0 s, el objetivo es la etapa `speech->stt`, no el VAD.

## Evidencia F3 - EN CURSO, panel humano pendiente

### Entregado

```
src/sincro/voices.py       M6  FishVoiceRegistry: enroll, get, remember, delete
src/sincro/enroll.py           make enroll / make enroll-delete, con consentimiento
src/sincro/synthesizer.py      reference_id -> voice_id del plugin
src/sincro/live.py             --neutral-voice, contraste en caliente
tests/make_ab.py               genera el material ciego del panel
tests/fixtures/voz_referencia.wav   clip de enrolamiento, 20.31 s, 16 kHz mono
out/ab/                        material del panel A/B
```

### Consentimiento: las dos rutas verificadas

El aviso se imprime **antes** de leer el clip, e informa de que el destino es un servidor
externo y de que la huella permite sintetizar habla que el hablante nunca dijo.

```
$ echo "n" | make enroll REF=tests/fixtures/voz_referencia.wav
  Autorizas subir este clip? [s/N]   Enrolamiento cancelado. No se subio nada.
make: *** [Makefile:69: enroll] Error 1
```

Ruta afirmativa:

```
$ make enroll REF=tests/fixtures/voz_referencia.wav SPEAKER=rodrigo
INFO httpx: HTTP Request: POST https://api.fish.audio/model "HTTP/1.1 201 Created"
INFO sincro.voices: enrolled speaker 'rodrigo' -> <reference_id redactado> (state=trained)

  reference_id : <reference_id redactado>
  speaker_id   : rodrigo
  visibilidad  : private
```

El `reference_id` real se omite de este documento a proposito: apunta a un modelo de
voz privado en Fish, y aunque el clip de F3 es sintetico (D19), publicar el de una voz
real filtraria un puntero a su huella vocal. Vive solo en `.env`, que no se versiona.

El clip no se copia a disco y el `reference_id` no se persiste: la cache de M6 vive solo
en memoria del proceso. `make enroll-delete ID=` borra la huella de Fish.

### Contraste A/B: medido, no supuesto

`make live` con y sin `--neutral-voice`, sobre el mismo WAV de entrada:

```
A) voz        : clonada <reference_id redactado>
B) voz        : neutra (por defecto de Fish)
```

No escuche los archivos. Se midio la frecuencia fundamental por autocorrelacion y el
centroide espectral sobre los tramos con energia:

```
archivo        F0 medio   centroide   etiqueta real
referencia      121.2 Hz      1215 Hz   (hablante, es)
muestra_1       126.9 Hz      1814 Hz   clonada
muestra_2       184.5 Hz      2443 Hz   neutra

distancia a la referencia:
  muestra_1 (clonada): dF0=  5.7 Hz   dCentroide=  599 Hz
  muestra_2 (neutra) : dF0= 63.3 Hz   dCentroide= 1228 Hz
```

La voz clonada queda a **5.7 Hz** de la referencia y la neutra a **63.3 Hz**: once veces
mas lejos en tono, y el doble de lejos en forma espectral. La salida del pipeline en vivo
reproduce el contraste: clonada 142.5 Hz, neutra 201.8 Hz, referencia 121.2 Hz.

Es prueba objetiva de que el `reference_id` transporta el timbre del hablante al idioma
destino. **No sustituye al panel humano**, que es el criterio.

### Lo que NO esta verificado: el criterio

F3 exige que **tres personas** escuchen A/B e identifiquen el timbre clonado. No puedo
escuchar audio ni convocar un panel. El material esta preparado como prueba **ciega**:

```
out/ab/referencia.wav     el hablante en espanol
out/ab/muestra_1.wav      \ mismo texto en ingles, orden barajado
out/ab/muestra_2.wav      /
out/ab/INSTRUCCIONES.txt  guion para quien administra el panel
out/ab/RESPUESTA.txt      la clave. NO abrir antes de que respondan
```

Las dos muestras dicen exactamente el mismo texto ingles, tomado de las traducciones
reales de la cascada de F1, de modo que la unica variable es el timbre.

**Resultado del panel: pendiente.**

| Oyente | Respuesta | Acierta |
|---|---|---|
| 1 | (pendiente) | |
| 2 | (pendiente) | |
| 3 | (pendiente) | |

### Salvedad de alcance

El clip de referencia es voz sintetica de una voz publica del catalogo de Fish (D19), no
voz humana. Por decision del usuario las voces reales se enrolan al final del proyecto.
Hasta entonces F3 valida el **mecanismo** de clonacion, no su calidad sobre voz humana.
Cuando se enrolen las voces reales hay que repetir el panel.

## Evidencia F4 - EN CURSO, criterio NO cumplido

### Entregado

```
src/sincro/drift.py        M8  DriftController + curva ASCII de deriva
src/sincro/dub_file.py         speed_for, should_drop y note_gap en la cascada offline
src/sincro/engine.py           lo mismo en el motor en vivo, con descarte registrado
tests/make_longform.py         genera el WAV de habla continua
tests/fixtures/es_10min.wav    651.1 s (10.85 min), 106 clausulas, pausas de 0.35 s
Makefile                       make drift-test
```

`DriftController` es byte a byte el del documento tecnico: `THRESHOLD_SOFT = 0.8`,
`THRESHOLD_HARD = 2.0`, `RESET_SILENCE = 1.5`, y `speed_for` / `should_drop` con la misma
logica. Lo unico anadido es `duration_ratio()`, que alimenta su parametro `budget_ratio`.

### `make drift-test`

```
segments   : 118
drift      : final -82.61s   |max| 82.85s   max atraso +0.00s   resets 0   drops 0
ratio dur. : 0.834 (1.000 = isocrono)

  curva de deriva (s)
    0 | ###############################################################
  -7.21|    ############################################################
  -12.62|        ########################################################
  -18.02|             ###################################################
  -23.42|                 ###############################################
  -28.82|                        ########################################
  -34.23|                           #####################################
  -39.63|                               #################################
  -45.03|                                    ############################
  -50.43|                                          ######################
  -55.84|                                             ###################
  -61.24|                                                 ###############
  -66.64|                                                     ###########
  -72.04|                                                        ########
  -77.45|                                                            ####
  -82.85|                                                               #
       ----------------------------------------------------------------
       segmento 1 .. 118
source     : 651.09s   dubbed: 519.59s   delta: -131.50s
```

### `make report`

```
SINCRO report - out/telemetry-20260826T070056.jsonl
118 segmentos   es -> en

Isocronia y deriva
  audio fuente      602.20 s
  audio doblado     519.59 s
  deriva final      -82.61 s
  deriva max        -82.85 s
  speed  min/max  0.950 / 1.000
  bytes  in/out   8342 / 6918   ratio 0.829

Coste
  tokens in/out   26243 / 1789
  total           $0.021113
  por minuto      $0.002104/min de habla
```

El TTFA de este reporte no es interpretable: la cascada offline transcribe el archivo
entero de una vez y procesa los 118 segmentos en serie. Ya anotado desde F1.

### El criterio, punto por punto

| Comprobacion | Resultado |
|---|---|
| Deriva acumulada < 300 ms por 10 min | **NO.** -82.61 s |
| `speed` dentro de 0.95-1.25 en el percentil 95 | **SI.** 118/118 segmentos en rango, p95 = 1.000 |

### Por que no cumple: es aritmetica, no un bug

```
habla fuente          : 602.20 s
ratio natural ES->EN  : 0.834   (el ingles dura el 83% de lo que dura el espanol)
piso de speed         : 0.95    (no negociable: protege el timbre clonado)
mejor ratio alcanzable: 0.834 / 0.95 = 0.878
deriva minima posible : (0.878 - 1) x 602.2 = -73.5 s

para |deriva| < 300 ms haria falta speed = 0.834, un 12% por debajo del piso
```

Medido -82.61 s contra un minimo teorico de -73.5 s: el controlador trabaja cerca de su
limite fisico, y ese limite esta a dos ordenes de magnitud del criterio.

### Lo que si cumple, y es lo que el controlador existe para garantizar

```
max atraso: +0.00 s
```

El doblaje **nunca va por detras** en los 10 minutos. El documento define `drift` como
"positivo significa que el doblaje va atrasado", y `THRESHOLD_SOFT` y `THRESHOLD_HARD`
existen para corregir esa direccion. La deriva negativa es **holgura**: el doblaje termina
antes y espera. En vivo la absorbe el silencio entre turnos.

### Un error propio, corregido midiendo

La primera corrida dio **-103.62 s** con `speed` llegando a 1.100. Le estaba pasando a
`speed_for` un ratio de **bytes** cuando el documento pide comparar **duraciones** (D22).
Como el ingles supera el presupuesto en bytes casi siempre, el ratio salia >1 y el
controlador **aceleraba** un audio que ya era demasiado corto. Corregido: -82.61 s y
`speed` en [0.950, 1.000].

### Rutas no ejercitadas por el test de 10 min

`drops 0` y `resets 0` no significan "funcionan". Verificadas aparte:

```
speed_for   drift 0.00 -> 0.950     drift 1.50 -> 1.105     drift 5.00 -> 1.250 (techo)
should_drop drift 2.5 + timeout + 12 chars -> True
            drift 2.5 + timeout + 40 chars -> False   (tiene contenido)
            drift 2.5 + eou     + 12 chars -> False   (no es relleno)
            drift 1.0 + timeout + 12 chars -> False   (bajo umbral duro)
RESET_SILENCE  hueco 0.5s -> no resetea     hueco 2.2s -> drift a 0.00
```

`should_drop` no dispara porque exige `drift > +2.0 s` y la deriva es negativa toda la
sesion. `RESET_SILENCE` no dispara porque el fixture tiene pausas de 0.35 s **a proposito**:
con pausas mayores la deriva se reseteria en cada frase y el test no mediria nada. En una
sesion real con pausas naturales si disparara.

## Evidencia F5 - EN CURSO, R1 sin concluir

### Entregado

```
src/sincro/matrix.py           make matrix-test: 20 pares, invariantes, recalibracion
src/sincro/config.py           expansion_for: medido > tabulado > derivado
tests/matrix_scripts.py        guion de 10 frases en los 5 idiomas + voces nativas
tests/fixtures/matrix_*.wav    5 WAV fuente, uno por idioma
out/expansion.json             12 factores medidos, los que config.py lee
out/matrix/matrix.json         detalle de los 20 pares
```

La tabla de los cinco idiomas (codigos Deepgram y turn-detector) ya estaba completa desde
F0; F5 solo la verifico. Los 8 pares con japones no llevan presupuesto de bytes, tambien
desde F0.

Optimizacion: la transcripcion no depende del idioma destino, asi que se hace **una vez
por fuente** y se reutiliza para los cuatro destinos. 5 llamadas a Deepgram en vez de 20.

### `make matrix-test` - los 20 pares dirigidos

```
par            seg ratio dur ratio byt  RT conf   invar
-------------------------------------------------------
es -> en        12     0.876     0.791    0.999   19/19
es -> pt-BR     12     1.171     0.926    0.998   19/19
es -> fr        12     1.031     0.899    0.997   19/19
es -> ja        12     1.132     1.347    0.999   17/19
en -> es        11     1.184     0.947    0.999   15/15
en -> pt-BR     11     1.113     0.938    0.997   15/15
en -> fr        11     1.040     0.993    0.997   15/15
en -> ja        11     1.424     1.377    0.999   15/15
pt-BR -> es     12     0.852     0.925    1.000   18/18
pt-BR -> en     12     0.675     0.796    1.000   18/18
pt-BR -> fr     12     0.793     0.932    0.997   18/18
pt-BR -> ja     12     0.915     1.372    0.999   18/18
fr -> es        10     0.925     0.870    1.000   17/17
fr -> en        10     0.723     0.748    0.999   17/17
fr -> pt-BR     10     0.844     0.862    0.998   17/17
fr -> ja        10     0.979     1.236    0.999   17/17
ja -> es        10     0.897     0.579    1.000     6/6
ja -> en        10     0.752     0.499    0.999     5/6
ja -> pt-BR     10     0.875     0.585    0.999     6/6
ja -> fr        10     1.026     0.660    0.998     6/6

  skip 1   leaked 0
```

**Inteligibilidad: los 20 pares pasan.** La confianza del round-trip STT sobre el audio
doblado va de 0.997 a 1.000. Ni un solo par produce audio que el STT no reconozca.

### Factores recalibrados y aplicados

Medidos sobre **duracion de audio real**, no sobre bytes teoricos, y escritos en
`out/expansion.json`. `config.expansion_for` los lee por delante de los del documento.

```
en->es      1.200 -> 1.013      fr->en      0.758 -> 1.048
en->fr      1.320 -> 1.269      fr->es      0.909 -> 0.982
en->pt-BR   1.200 -> 1.079      fr->pt-BR   0.909 -> 1.077
es->en      0.850 -> 0.971      pt-BR->en   0.833 -> 1.235
es->fr      1.100 -> 1.066      pt-BR->es   1.000 -> 1.174
es->pt-BR   1.000 -> 0.854      pt-BR->fr   1.100 -> 1.387
```

Los valores del documento se desvian bastante de lo medido: `pt-BR->fr` pasa de 0.909 a
1.387 y `fr->en` de 0.758 a 1.048. Los teoricos eran una primera aproximacion por bytes,
y la duracion cuenta otra historia. Los 8 pares con japones no se recalibran: no usan
presupuesto de bytes.

**Salvedad:** esta es una sola iteracion de calibracion. Aplicar los factores cambia el
presupuesto, que cambia las traducciones, que cambiarian las duraciones medidas. Conviene
una segunda pasada para ver si converge.

### Riesgo R1: medido, pero SIN CONCLUIR

```
con japones : 8 pares   invariantes 90/93 = 96.8%   RT medio 0.9990
sin japones : 12 pares  invariantes 207/207 = 100%  RT medio 0.9984
```

A primera vista R1 no se materializa: los pares con japones puntuan casi igual y su
confianza de round-trip es incluso algo mayor. **Pero ese numero no es fiable**, por dos
bugs que encontre investigando por que el japones fuente marcaba 6/19:

- **D28**: `committer.py` unia los morfemas japoneses con espacios, asi que al traductor
  le llegaba `"20 2 6 年 の 第 3 四半 期"` en vez de `"2026年の第3四半期"`. Afecta a los
  cuatro pares `ja -> *`. **Es un bug del motor, no del instrumento de medida.**
- **D29**: mi comparador borraba el dakuten al quitar acentos, convirtiendo `ロドリゴ` en
  `ロトリコ`. Falsos negativos en los nombres propios.

Ambos estan **corregidos y verificados** en local: sobre texto japones real el comparador
pasa de 6/19 a 19/19 y el dakuten se conserva.

Lo que **no** pude hacer es volver a medir. Al relanzar la matriz salto el limite:

```
429 - Rate limit reached for `qwen/qwen3.6-27b` on tokens per day (TPD):
Limit 200000, Used 199761
```

**Cuota diaria de Groq agotada** (D30). Con los bugs presentes, los pares `ja -> *` se
puntuaron contra solo 6 invariantes de 19, lo que infla su porcentaje.

**Conclusion honesta:** hay indicios fuertes de que qwen3.6-27b maneja bien el japones
(RT 0.999 en los ocho pares, kanji y katakana correctos en las inspecciones manuales,
`二千二十七年三月十五日` y `四万八千二百九十一` bien traducidos), pero **R1 no se cierra
hasta remedir**. No se asume ni roto ni funcional.

### Lo que falta para cerrar F5

1. Volver a correr `make matrix-test --apply` con los arreglos D28/D29, manana o con
   cuota. Basta `ONLY=ja` mas los cuatro destinos a japones para lo afectado.
2. Segunda iteracion de calibracion, para ver si los factores convergen.
3. Panel humano de inteligibilidad: el round-trip STT prueba que el audio es reconocible,
   no que suene natural.

## Evidencia F6 - HECHO

### Entregado

```
src/sincro/transcriber.py   reconexion con backoff, buffer de 30 s y ventana de reenvio
src/sincro/engine.py        degradacion a subtitulo si cae el TTS
src/sincro/soak.py          make soak: sesion larga con corte de red inyectado
src/sincro/report.py        curva de deriva ASCII en la tabla final
docs/demo.md                guion de demo por consola de 3 min
```

### `make soak MIN=20 CUT_AT=10` - criterio de aceptacion

```
  duracion real   : 20.0 min
  turnos          : 195
  TTFA            : P50 7998 ms   P90 16843 ms   P99 21553 ms
  triggers        : {'eou': 102, 'timeout': 62, 'punctuation': 31}
  deriva          : final +23.11s   max atraso +33.05s
  reconexiones    : 1   sin socket 4.9s   frames reenviados 226
  buffer          : 0 frames descartados por desbordamiento
  TTS degradado   : 0 turnos a subtitulo
  gate            : mute_calls 196, unmute_calls 195

  CRITERIO F6
    sesion completa sin intervencion : SI
    la sesion no se cayo             : SI
    corte de red recuperado solo     : SI (1 reconexiones)
    turnos despues del corte         : SI
```

**Sin perder segmentos.** 195 segmentos con ids 1..195 y **ningun hueco en la numeracion**.
El corte midio 4.9 s contra los 5 s pedidos.

Recuperacion visible en los datos, con el TTFA decayendo mientras se procesa el reenvio:

```
seg 100  t=584.5s  timeout      ttfa=  8702     <- ultimo antes del corte
>>> [10.0m] CORTE DE RED SIMULADO, 5s <<<
seg 101  t=592.6s  eou          ttfa= 15749     <- primero despues, con el buffer encima
seg 102  t=597.9s  punctuation  ttfa= 13582
seg 103  t=604.7s  eou          ttfa= 12266
seg 104  t=610.3s  eou          ttfa= 10804     <- vuelve a la normalidad
```

### Degradacion del TTS, verificada aparte

Con un sintetizador que lanza `SynthesisError` en cada turno:

```
  turno 1 [SUBTITULO: TTS caido]
     Good morning everyone, and thank you for joining this morning's meeting.

  la sesion se cayo?      : NO
  turnos producidos       : 1
  degradados a subtitulo  : 1
  puerta anti-eco         : mute=1 unmute=1
```

La traduccion sale por pantalla y la sesion continua. El turno degradado no alimenta la
calibracion de duracion del controlador de deriva: un audio de cero segundos falsearia el
ratio de los turnos siguientes.

### `make report` - tabla final con curva de deriva

```
195 segmentos   es -> en

TTFA
  P50      7998 ms      P90     16843 ms      P99     21553 ms

Triggers
  !! eou          102   52.3%   objetivo 55%-70%
  !! punctuation   31   15.9%   objetivo 20%-35%
  !! timeout       62   31.8%   objetivo 0%-10%
  ok max_len        0    0.0%   objetivo 0%-3%

Isocronia y deriva
  audio fuente      891.82 s      audio doblado     957.95 s
  deriva final      +23.11 s      deriva max        +33.05 s
  speed  min/max  1.000 / 1.250

  curva de deriva (s)
  +33.05|                                #
  +24.41|                        #########
  +15.78|              ###################                     ##########
  +7.14|     ############################     ###     ##################
    0 |################################################################
       ----------------------------------------------------------------
       segmento 1 .. 195
```

### Dos ramas del controlador de deriva ejercitadas por primera vez

D24 dejo anotado que `make drift-test` no llegaba a probarlas, porque en F4 la deriva era
negativa toda la sesion. Aqui si:

- **Rama de aceleracion.** `speed` alcanza el techo duro de 1.25 en **186 de 195**
  segmentos, y se queda en rango en **195/195**. Es la respuesta correcta a una deriva de
  +33 s.
- **`RESET_SILENCE`.** Dispara dos veces, y **la primera coincide con el corte de red**:

  ```
  seg 100 -> 101:  +31.82s -> +1.73s   (recupera 30.09s)  <- a los 9.9 min, el corte
  seg 124 -> 125:  +11.20s ->  +0.66s   (recupera 10.54s)
  ```

  El silencio del corte supera los 1.5 s de `RESET_SILENCE` y borra la deuda acumulada.
  Es el mecanismo funcionando tal como el documento lo describe.

`should_drop` **sigue sin dispararse**, pese a 190 segmentos por encima del umbral duro:
exige ademas `trigger == 'timeout'` y menos de 25 caracteres, y el fixture nunca produce
segmentos cortos de relleno. Esa rama solo se ejercitara con habla conversacional real.

### Salvedades de esta medida

**El traductor es falso.** La cuota diaria de Groq se agoto a mitad de F6 (D30), asi que
los 20 minutos corrieron con `--offline-llm` (D34): VAD, WebSocket con su reconexion,
triggers, deriva y TTS son todos reales; **M5 no**. Consecuencias al leer los numeros:

- **TTFA no es interpretable.** El P90 de 16.8 s refleja el atasco por deriva positiva,
  no la latencia del pipeline real. El `min -911 ms` es el margen del mapeo de tiempos
  aflorando cuando la cascada es casi instantanea (D35).
- **La deriva positiva es artificial.** `FakeTranslator` invierte el texto y Fish lo
  pronuncia mas despacio que una frase real. Util para ejercitar el controlador, inutil
  para juzgar isocronia.
- **La distribucion de triggers no es representativa**, ni el coste: los tokens salen de
  las estimaciones del fake, no de Groq.

Lo que **si** mide esta corrida, que es lo que el criterio de F6 pide: 20 minutos sin
intervencion, sin caida, con recuperacion automatica del corte y sin perder segmentos.

**Un desajuste menor:** `mute_calls 196` contra `unmute_calls 195`. El ultimo turno
quedaba reproduciendo cuando la sesion cerro y su `unmute` diferido se cancelo. Corregido:
`engine.aclose()` espera ahora las tareas pendientes antes de cerrar.

### `docs/demo.md`

Guion de 3 minutos en cuatro bloques, en el orden pedido: traduccion funcionando,
contraste de voz clonada contra neutra, frase con numeros y fechas exactos, y tabla de
metricas. Incluye checklist previo, plan B si se cae la red, y una seccion de **lo que no
hay que prometer** con los criterios que siguen sin cumplirse.

## Registro de sesiones

### 2026-08-26 - F6 cerrada

Reconexion del WebSocket con backoff, buffer de 30 s y ventana de reenvio de 10 s;
degradacion a subtitulo si cae el TTS; `make soak` con corte de red inyectado; curva de
deriva en `make report`; y `docs/demo.md`.

**Criterio cumplido:** 20.0 min sin intervencion, 195 segmentos sin huecos en la
numeracion, 1 reconexion automatica tras un corte de 4.9 s, 226 frames reenviados y 0
perdidos por desbordamiento.

Salvedad importante: los 20 minutos corrieron con `--offline-llm` porque la cuota de Groq
se agoto (D34). Todo el pipeline es real salvo M5. El TTFA y la deriva de esa corrida no
son interpretables; lo que mide es estabilidad y recuperacion, que es el criterio.

Efecto util: la deriva positiva ejercito por primera vez la rama de aceleracion del
controlador (speed en el techo de 1.25 en 186/195) y `RESET_SILENCE`, que disparo
justo en el corte de red y recupero 30.09 s de deuda. `should_drop` sigue sin probarse.

F5 se cierra por decision del usuario con R1 sin concluir.

Cinco desvios: D31 (corte simulado en el socket), D32 (buffer y ventana de reenvio),
D33 (degradacion a subtitulo), D34 (`--offline-llm`) y D35 (TTFA negativo).

### 2026-08-26 - F5 en curso, R1 sin concluir

`matrix.py` con los 20 pares dirigidos, guion de 10 frases en los cinco idiomas y
recalibracion por duracion medida. Los 20 pares producen audio inteligible (RT 0.997-1.000)
y 12 factores quedan medidos y aplicados en `out/expansion.json`.

**F5 NO se marca HECHO y R1 NO se cierra.** Investigando por que el japones puntuaba mal
aparecieron dos bugs: uno del motor (D28, el committer unia morfemas japoneses con
espacios y le mandaba texto roto al traductor) y uno del instrumento (D29, el comparador
borraba el dakuten). Ambos corregidos y verificados en local, pero **no remedidos**: la
cuota diaria de Groq se agoto a mitad del relanzamiento (D30).

F4 se cierra por decision del usuario con el criterio literal sin cumplir.

### 2026-08-26 - F4 en curso, criterio no cumplido

`drift.py` implementado tal como el documento y conectado a la cascada offline y al motor.
`make drift-test` corre sobre un WAV de 10.85 min generado para la ocasion.

**F4 NO se marca HECHO.** Deriva -82.61 s frente al criterio de 300 ms. No es un fallo de
implementacion: con el ratio natural ES->EN de 0.834 y el piso de `speed` en 0.95, el
minimo alcanzable es -73.5 s (D23). El criterio y la regla del piso de velocidad son
incompatibles para este par.

Lo que si cumple: **atraso maximo +0.00 s**, el doblaje nunca va por detras, y `speed` en
rango en 118/118 segmentos.

Un error propio corregido midiendo: pasaba a `speed_for` un ratio de bytes en vez de
duraciones, lo que aceleraba audio que ya era corto (D22). De -103.62 s a -82.61 s.

F3 se cierra por decision del usuario con el panel aun pendiente.

### 2026-08-26 - F3 en curso

Construidos los cuatro puntos: `voices.py` (M6), `make enroll` con consentimiento,
`reference_id` conectado al sintetizador y `--neutral-voice` en `make live`.

F2 se cierra por decision del usuario sin su medida por microfono; queda anotado en D18.

**F3 NO se marca HECHO.** Su criterio es un panel de tres personas escuchando A/B, y eso
lo ejecuta el usuario. El material esta generado como prueba ciega en `out/ab/`.

Lo que si esta verificado: enrolamiento real contra Fish (HTTP 201, `state=trained`), las
dos rutas del consentimiento, y el contraste de timbre medido por F0 y centroide espectral
(clonada a 5.7 Hz de la referencia, neutra a 63.3 Hz).

Cuatro desvios: D18 (cierre de F2), D19 (clip de referencia sintetico), D20 (creacion del
modelo por REST) y D21 (`SINCRO_VOICE_ID`).

### 2026-08-26 - F2 en curso

Construidos los seis puntos: `gate.py`, WebSocket en `transcriber.py`, los 4 triggers en
`committer.py`, streaming en `synthesizer.py`, `console_io.py` y `engine.py`. `make live`
avisa de auriculares antes de abrir el microfono.

**F2 NO se marca HECHO.** Su criterio exige voz real por microfono durante 5 minutos y 20
turnos, y eso lo tiene que ejecutar el usuario. Lo que se verifico es el pipeline, con un
banco `--from-wav` (D17).

Cuatro desvios: D14 (turn-detector como runner local, porque el reemplazo oficial es un
gateway cloud que romperia el "local, $0" del stack), D15 (frontera con `livekit.rtc`),
D16 (semantica de `unlikely_threshold`, error propio corregido) y D17 (banco de pruebas).

Dos errores propios encontrados y corregidos por medicion, no por suposicion: el TTFA
estaba inflado por sumar la linea temporal de Deepgram al reloj de pared sin traducirla
(P90 8030 -> 3592 ms), y el turn-detector se usaba como compuerta de commit en vez de como
umbral de espera, lo que cortaba a media frase.

F0 y F1 sin regresion.

### 2026-08-26 - Correccion de F1: modelo de traduccion

Correccion pendiente de F1, aplicada antes de abrir F2. **No se inicio F2.**

F1 seguia HECHO pero su evidencia corrio con un modelo sustituto sin validar. Se migro a
`qwen/qwen3.6-27b` con `reasoning_effort=none` y se revalido el criterio entero.

Cambios:

- `config.py`: cuatro parametros nuevos leidos de `.env` (`SINCRO_LLM_MODEL`,
  `SINCRO_LLM_REASONING_EFFORT`, `SINCRO_LLM_TEMPERATURE`, `SINCRO_LLM_MAX_TOKENS`).
- `translator.py`: los usa, y detecta fuga de marcadores de control al contenido.
- `telemetry.py`: `LLM_PRICES` por nombre de modelo, unica fuente de tarifas, tomada del
  endpoint `/models` de Groq. `llm_cost_usd()` recibe ahora el modelo.
- `.env` y `.env.example`: valores y el porque.
- D8 pasa de parche a decision cerrada. D12 registra el riesgo R8. D13 deja la directiva
  de F2 sobre el SDK de Deepgram, sin implementarla.

Fuera de alcance, no tocado: `engine.py`, WebSocket de M3, triggers `eou`/`max_len`,
guardas de no-corte, fixture, esquema JSONL.

Resultado: gate de tokens **VERDE** con 16.2 por clausula, las 6 traducciones correctas,
0 fugas de marcadores. F1 sigue HECHO con evidencia nueva.

### 2026-08-25 - F1 cerrada

Cascada offline sobre archivo. M3, M4, M5 y M7 implementados; M2 y M6 siguen pendientes
porque F1 no los necesita.

Cinco desvios registrados: D7 (cliente REST de Deepgram en vez del plugin, porque el
plugin descarta `Word.confidence`), D8 (`llama-3.3-70b-versatile` no disponible en Groq),
D9 (sesion HTTP propia para los plugins standalone), D10 (fixture generado por TTS) y
D11 (modulos de entrada `dub_file.py` y `report.py`).

### 2026-08-25 - Reconciliacion de D1, post-F0

Correccion pendiente de F0, aplicada antes de abrir F1. **No se inicio F1.**

`TranscriptEvent` ya figura en Notion, Documentacion tecnica seccion 2. Se reconcilio
`contracts.py` con esa forma y se cerro D1 en `DECISIONS.md`.

Cambios:

- `contracts.py`: `TranscriptEvent` pasa de 6 campos planos a la forma de Notion:
  `text`, `lang`, `is_final`, `speech_final`, `words`, `t_emit`, con `t_start` y `t_end`
  como propiedades derivadas de `words`. Se anade la dataclass `Word`. Ambas se colocan
  despues de `DubbedChunk`, como en el documento.
- `fakes.py`: `FakeTranscriber` emite la forma nueva. Parciales con `words=[]`; finales
  con `words` repartido por longitud de palabra y bordes exactos. `speech_final` derivado
  de puntuacion de cierre, no de `is_final`.
- No se toco ningun otro archivo. No se creo `engine.py` ni fixtures. No se implemento
  ningun modulo real.

Verificacion:

```
$ make lint
.venv/bin/ruff check src/
All checks passed!
.venv/bin/mypy
Success: no issues found in 7 source files
```

```
$ make check
[1/2] credentials
  PASS  deepgram  HTTP 200
  PASS  groq      HTTP 200
  PASS  fish      HTTP 200
[2/2] fake pipeline telemetry
  PASS  6 segments, 6/6 stage marks each
RESULT: PASS - 3/3 credentials valid, telemetry JSONL emitted.
EXIT=0
```

El JSONL emitido es identico al de F0 campo a campo, ignorando marcas de reloj y
`ttfa_ms`: la reconciliacion cambia la estructura del evento sin mover la telemetria.
`source_duration_s` sigue en 3.0 porque los bordes de `words` caen exactamente en los
bordes de la clausula.

Semantica verificada:

```
'Buenos dias a todos.' -> is_final=True speech_final=True
'y luego dijimos que,' -> is_final=True speech_final=False   <- compuerta abierta, endpointing no
parcial                -> words=[]  t_start=0.0  t_end=0.0
determinismo en 2 corridas: True
```

Ningun estado de fase ni de modulo cambio. F0 sigue HECHO.

## Bloqueos activos

(ninguno)

## Preguntas abiertas

### R1 (reescrito 2026-08-26) - calidad multilingue del modelo de traduccion

La redaccion anterior era: "calidad de Llama 3.3 70B en pares con japones". Queda
obsoleta: Llama 3.3 70B ya no existe en Groq (D8), y el riesgo estaba construido sobre su
lista de idiomas oficiales (en, de, fr, it, pt, hi, es, th).

Con `qwen/qwen3.6-27b` hay que **reevaluar los cinco idiomas en F5**, no solo japones: el
modelo cambio entero, no solo para un par. Los 20 pares dirigidos se miden de cero.

El cambio podria **reducir** R1 en vez de agravarlo: los modelos Qwen son notablemente mas
fuertes en japones que Llama 3.3, que ni siquiera lo listaba. Es una hipotesis razonable,
no un resultado. **Se mide en F5 con el guion fijo de 10 frases con numeros, fechas y
nombres propios. No se asume ni roto ni funcional.**

### R8 (nuevo 2026-08-26) - qwen/qwen3.6-27b es un modelo Preview en Groq

Groq lo clasifica bajo Preview Models: "intended for evaluation purposes only and should
not be used in production environments as they may be discontinued at short notice".
`openai/gpt-oss-120b` esta bajo Production Models. La contradiccion es de Groq: su pagina
de deprecaciones recomienda el qwen como destino de migracion desde Llama 3.3.

El endpoint `/models` no expone el estado preview (`"active": true`, sin campo de
deprecacion), asi que esto no se puede vigilar de forma programatica.

Riesgo: otra deprecacion a mitad de proyecto, esta vez con aviso corto en vez de los dos
meses que dio Groq con Llama 3.3.

Alternativa conservadora, a un cambio de linea en `.env`: `openai/gpt-oss-120b` con
`reasoning_effort="low"`, que esta en Production y **tambien pasa el gate de tokens**
(28 de salida, 5 de razonamiento, contra 18 de qwen3.6), y cuesta 5 veces menos por token
de salida. **Requiere decision del usuario.** Ver D12.

### R9 (nuevo 2026-08-26) - el TTS pronuncia mal los nombres propios extranjeros

`Arequipa` vuelve del round-trip como `Araquipa` con qwen3.6-27b, y volvia como `Arkhipa`
con gpt-oss-120b.

**El invariante de traduccion SI se preservo:** el texto en ingles dice `Arequipa`. Lo que
falla es que la voz TTS inglesa aplica reglas de grafema-a-fonema del ingles a un nombre
propio espanol. Es un problema de **pronunciacion del TTS**, no de traduccion ni de STT.

Afecta directamente al argumento de transportar nombres propios sin error: el texto esta
bien y el audio los dice mal. Para F5 y F6.

### R10 (nuevo 2026-08-26) - el criterio de F4 y el piso de speed son incompatibles

Ver D23. Tres salidas, ninguna elegible sin decision del usuario:

1. **Reinterpretar el criterio** como "atraso acumulado < 300 ms". F4 cumpliria hoy con
   +0.00 s. Es lo que recomiendo: mide la direccion que hace dano.
2. **Bajar el piso de speed** de 0.95 a ~0.83. Contradice una regla no negociable de
   CLAUDE.md y degrada el timbre que F3 acaba de conseguir.
3. **Rellenar con silencio** en vez de estirar el habla, como el doblaje profesional.
   No toca `speed` ni el timbre, pero cambia el contrato de M7 y es trabajo no previsto.

### Otras abiertas

- `cost_usd` ya usa tarifas por modelo (`telemetry.LLM_PRICES`, del endpoint `/models`).
  Revisar la tabla si se cambia de modelo o si Groq cambia precios.
- Groq expone `input_cache_read` en el precio de qwen3.6-27b, lo que sugiere que **si**
  hay prompt caching en este modelo. R6 estaba redactado sobre Llama 3.3, que no lo tenia.
  Si se confirma, el limite de 200 tokens de system prompt podria relajarse. Medir antes
  de cambiar nada.
- `qwen/qwen3.8-27b` aparecio en la cuenta el 2026-08-26 (no estaba el dia anterior).
  Mas caro (0.80 / 4.00 contra 0.60 / 3.00) y sin evaluar.
- El fixture es voz sintetica (D10). Repetir `make dub-file` con una grabacion real antes
  de dar por buena la robustez de M3.
- Escucha humana del WAV doblado pendiente de confirmar.
