# DECISIONS - SINCRO Engine v3

Registro de desvios del plan y de decisiones no derivables de la documentacion.
Formato: fecha, decision, razon.

## 2026-08-25 - F0

### D1. `TranscriptEvent` definido en contracts.py - RECONCILIADO 2026-08-25

**Estado: cerrado.** La definicion provisional se reemplazo por la que quedo en Notion,
Documentacion tecnica seccion 2. `contracts.py` ya no diverge del documento.

Origen: el documento referenciaba `TranscriptEvent` en los Protocols de M3 y M4 como
forward reference pero no incluia su dataclass, asi que se escribio una provisional.

Que cambio respecto a la version provisional:

| | Provisional (F0 inicial) | Reconciliada (Notion §2) |
|---|---|---|
| `speech_final` | **no existia** | `bool`, senal separada de `is_final` |
| `words` | **no existia** | `list[Word]`, vacio en parciales |
| `t_start` / `t_end` | campos `float` asignados por el productor | **propiedades** derivadas de `words[0].start` y `words[-1].end` |
| `t_emit` | no existia | `float` monotonic, para telemetria |
| `confidence` | `float = 1.0` a nivel de evento | movido a `Word.confidence`, por palabra |
| `Word` | no existia | dataclass nueva: `text`, `start`, `end`, `confidence` |
| orden en el archivo | entre `SpeechFrame` y `Segment` | despues de `DubbedChunk`, como en Notion |

Las tres razones por las que estos campos son obligatorios y no se deben simplificar:

1. **`is_final` y `speech_final` son senales distintas.** `is_final` significa que
   Deepgram cerro el fragmento y no cambiara; `speech_final` significa que el endpointing
   interno de Deepgram detecto que el hablante paro. Colapsarlas hace que M4 acabe usando
   el endpointing de Deepgram como trigger de commit, y ese umbral no se configura desde
   el motor: la politica de commit dejaria de ser nuestra y el trigger `eou` del
   turn-detector quedaria redundante. Notion §5 lo fija: `is_final` es la **compuerta**,
   no un trigger; `speech_final` solo corrobora `eou` y `punctuation`.
2. **`words[]` no es opcional.** `t_start` y `t_end` del `Segment` se derivan de los
   timestamps por palabra, nunca de la hora de llegada del paquete. Alimentan
   `source_duration`, que alimenta el presupuesto de bytes y el `DriftController`.
   Derivarlos del reloj de pared mete el jitter de red Arequipa-US dentro del calculo de
   isocronia, que es exactamente lo que la isocronia intenta corregir.
3. **Tiene que sobrevivir a F2.** F1 usa Deepgram pre-grabado y F2 migra a WebSocket; las
   dos APIs devuelven formas distintas. Ajustar `TranscriptEvent` a la forma del modo
   pre-grabado obligaria a reescribirlo en F2, y con el M4 entero.

**No se conservo ningun campo adicional.** El unico que tenia la version provisional,
`confidence` a nivel de evento, no era una aportacion sino un sustituto de la confianza
por palabra, que no existia porque no existia `words[]`. Ahora que `Word.confidence` la
lleva, el campo de evento seria dato duplicado y derivable. Se elimina en vez de
conservarse. Si M5 necesita un numero por fragmento para la valvula `[[SKIP]]` de
`prompts/translate.md`, se calcula desde `words` en F1, donde se sabra que agregacion
hace falta.

`FakeTranscriber` se actualizo a la forma nueva: los parciales llegan con `words=[]` como
en Deepgram, los finales llevan `words` repartido en proporcion a la longitud de cada
palabra, con el primer `start` y el ultimo `end` exactamente en los bordes de la clausula
para que `source_duration` no acumule error de redondeo. `speech_final` del fake se deriva
de la puntuacion de cierre de frase, no de `is_final`, de modo que el fake puede emitir
`is_final=True` con `speech_final=False` y M4 pueda probar la distincion en F2.

### D2. Python 3.13 en `.venv`, no el interprete del sistema

Arch trae Python 3.14.5, fuera del rango 3.11-3.13 que exige la documentacion (seccion 10).
`make setup` crea `.venv` con un 3.13 gestionado por `uv`, y todos los targets del Makefile
usan `.venv/bin/python`. No se cambia el rango soportado.

### D3. `httpx` anadido a las dependencias

No figura en la lista de la seccion 10. Lo requiere el healthcheck de credenciales de
`make check`, que hace una llamada HTTP directa a cada proveedor en vez de instanciar los
plugins de LiveKit: mas rapido, sin descarga de pesos y con un mensaje de error que
distingue credencial ausente, credencial rechazada y proveedor inalcanzable.
Ya venia como dependencia transitiva de `livekit-agents`; se declara explicita.

### D4. Endpoint de validacion de Fish Audio

`GET https://api.fish.audio/model` responde **200 con una credencial invalida** (es un
catalogo publico), por lo que no sirve como healthcheck. Verificado:

```
GET /model?page_size=1   con token invalido -> 200
GET /wallet/self/api-credit  con token invalido -> 401 {"status":401,"message":"Invalid token"}
```

`make check` usa `/wallet/self/api-credit`. Deepgram usa `/v1/projects` y Groq
`/openai/v1/models`; ambos devuelven 401 con credencial invalida.

### D5. Factores de expansion para los pares no tabulados

La seccion 3 tabula 4 pares (EN-ES 1.20, ES-EN 0.85, ES-PT 1.00, ES-FR 1.10) y declara
que los pares con japones no usan presupuesto de bytes. Los 20 pares dirigidos necesitan
un valor. Se implementa en `config.py`:

- Los 4 pares tabulados usan su valor literal.
- Los pares con `ja` en origen o destino devuelven `0.0` (`BYTE_BUDGET_DISABLED`), y
  `uses_byte_budget()` devuelve False. El control es solo por duracion.
- El resto se deriva de pesos de verbosidad relativos (`en` 1.00, `es` 1.20, `pt-BR` 1.20,
  `fr` 1.32) calibrados contra los valores tabulados.

Son provisionales por construccion. **F5 los reemplaza con valores medidos** sobre duracion
de audio real, no con estos.

### D6. `contracts.py` importa `AsyncIterator` de `collections.abc`

La documentacion lo importa de `typing`, deprecado desde 3.9. Los tipos son identicos y
las firmas de los Protocols no cambian. Exigido por `ruff UP035`.

## 2026-08-25 - F1

### D7. M3 no usa `livekit-plugins-deepgram`: cliente REST propio

`Word.confidence` es obligatorio por D1, y el plugin lo descarta. Su conversion construye
`TimedString`, que es una subclase de `str` con `start_time` y `end_time` y **sin campo de
confianza**, tanto en el camino pre-grabado (`prerecorded_transcription_to_speech_event`)
como en el de streaming (`live_transcription_to_speech_data`). Verificado en
livekit-plugins-deepgram 1.7.0:

```
$ python -c "from livekit.agents.types import TimedString; print(TimedString.__mro__)"
(<class 'livekit.agents.types.TimedString'>, <class 'str'>, <class 'object'>)
```

`transcriber.py` llama a `POST /v1/listen` con httpx y conserva `words[].confidence` tal
como la devuelve Deepgram. El proveedor y el modelo del stack no cambian: sigue siendo
Deepgram Nova-3 Monolingual con idioma fijo. Lo que no se usa es el envoltorio del plugin.

**Pendiente para F2:** el camino de streaming del plugin tiene el mismo problema, asi que
F2 tendra que decidir entre un cliente WebSocket propio o aceptar perder la confianza por
palabra. No se resuelve aqui.

### D8. Modelo de traduccion: `qwen/qwen3.6-27b` con `reasoning_effort=none` - CERRADO 2026-08-26

**Estado: decision cerrada.** Sustituye al parche provisional a `gpt-oss-120b`.

**Causa real.** No era un problema de cuenta. Groq anuncio la deprecacion de
`llama-3.3-70b-versatile` el 17/06/2026 y lo apago el 16/08/2026. Su propia pagina de
deprecaciones da como destinos de migracion `openai/gpt-oss-120b` o `qwen/qwen3.6-27b`.
El 404 es permanente. La especificacion del stack estaba desactualizada.

**Por que gpt-oss-120b no servia como stack definitivo.** Corrio en su
`reasoning_effort` por defecto (`medium`) y emitio 3504 tokens de salida para 6
clausulas, 584 por clausula. Esos tokens se generan **antes** de la traduccion, asi que
no se solapan con nada: el TTS no puede arrancar hasta que el modelo termina de pensar.
Son entre 0.6 y 1.2 s de latencia pura por clausula sobre un presupuesto total de 2.0 s
para el TTFA P90 de F2.

`include_reasoning=false` y `reasoning_format="hidden"` **no** son solucion: controlan si
el razonamiento aparece en la respuesta, no si se genera. Los tokens se producen y se
facturan igual, y ademas desaparecen de la telemetria, que empeora el diagnostico.

**Escalera de verificacion (2026-08-26).** Se detuvo en el peldano 1.

| # | Configuracion | Resultado |
|---|---|---|
| 1 | `qwen/qwen3.6-27b` + `reasoning_effort="none"` | **ACEPTADO.** 18 tokens de salida, sin razonamiento, contenido limpio |
| 2 | `qwen/qwen3.6-27b` sin `reasoning_effort` | **FALLA.** No probado como solucion, pero medido: el default NO es `none` |
| 3 | `openai/gpt-oss-120b` + `reasoning_effort="low"` | No hizo falta. Medido igualmente: 28 tokens de salida, 5 de razonamiento |

El peldano 2 merece detalle porque contradice el supuesto de partida. Sin
`reasoning_effort`, qwen3.6-27b **emite el razonamiento dentro del propio `content`**
como un bloque `<think>` literal, no en un campo separado, y agota `max_tokens`:

```
content      : '\n<think>\nHere's a thinking process:\n\n1. **Analyze User Input:** ...'
tokens       : in=62 out=200
finish_reason: length
```

Ese texto habria llegado al TTS y se habria reproducido en voz alta. `max_tokens=200`
hizo exactamente su trabajo de red de seguridad.

Ademas, `qwen/qwen3.6-27b` **solo acepta `none` o `default`**, no la escala low/medium/high:

```
reasoning_effort="medium" -> 400 {"message": "`reasoning_effort` must be one of `none` or `default`"}
```

La referencia de la API documenta la escala sobre `qwen/qwen3.8-27b`, que es otro modelo.
No se puede extrapolar de una version a otra.

**Configuracion, toda en `.env`:**

```
SINCRO_LLM_MODEL=qwen/qwen3.6-27b
SINCRO_LLM_REASONING_EFFORT=none
SINCRO_LLM_TEMPERATURE=0.2
SINCRO_LLM_MAX_TOKENS=200
```

El plugin de OpenAI solo autoconfigura `reasoning_effort` para los modelos gpt-5.x, asi
que para qwen hay que pasarlo explicito o el modelo razona por defecto.

**Resultado medido:** 16.2 tokens de salida por clausula (max 18), frente a 584 antes.
Reduccion de 36x, y por debajo de los 28 que estimaba el presupuesto original del
proyecto. Las 6 traducciones siguen siendo correctas.

### D12. `qwen/qwen3.6-27b` es un modelo Preview en Groq - RIESGO R8

Groq clasifica `qwen/qwen3.6-27b` bajo **Preview Models**, con esta advertencia:

> "Preview models are intended for evaluation purposes only and should not be used in
> production environments as they may be discontinued at short notice."

`openai/gpt-oss-120b` esta bajo **Production Models**. La contradiccion es de Groq: su
pagina de deprecaciones recomienda el qwen como destino de migracion desde
llama-3.3-70b-versatile, mientras su pagina de modelos lo marca como no apto para
produccion.

El endpoint `/models` **no** expone el estado preview. Devuelve `"active": true` y ningun
campo de deprecacion, asi que no sirve para detectar esto de forma programatica:

```json
{"id": "qwen/qwen3.6-27b", "active": true, "owned_by": "Alibaba Cloud",
 "context_window": 131072, "max_completion_tokens": 16384,
 "hugging_face_id": "Qwen/Qwen3.6-27B",
 "input_modalities": ["text", "image"], "output_modalities": ["text"],
 "pricing": {"prompt": "0.0000006", "completion": "0.000003",
             "input_cache_read": "0.0000003"},
 "supported_sampling_parameters": ["temperature","top_p","stop","seed","max_tokens"],
 "supported_features": ["tools","json_mode","reasoning"]}
```

**Riesgo R8:** podriamos comernos otra deprecacion a mitad del proyecto, esta vez con
"limited advance warning" en vez de los dos meses que dio Groq con Llama 3.3.

**Alternativa conservadora, si R8 se materializa o si se decide no correrlo:**
`openai/gpt-oss-120b` con `reasoning_effort="low"`. Esta en Production y **tambien pasa
el gate de tokens**: 28 de salida con 5 de razonamiento en la clausula de prueba, frente
a 18 de qwen3.6. El coste por token de salida es ademas 5 veces menor (0.60 contra 3.00
por millon). El cambio es de una linea en `.env`, sin tocar codigo.

Dato adicional: `qwen/qwen3.8-27b` **tambien aparece ya en la cuenta** (no estaba el
2026-08-25). Es mas caro (0.80 / 4.00) y no se ha evaluado.

### D9. Los plugins de LiveKit necesitan una `aiohttp.ClientSession` propia

Fuera del agent worker, el plugin de Fish falla con:

```
RuntimeError: Attempted to use an http session outside of a job context.
```

`FishSynthesizer` crea su propia `aiohttp.ClientSession`, la pasa al constructor como
`http_session=` y la cierra en `aclose()`. Es la via que el propio error documenta para
uso standalone, y es coherente con la regla de no usar `AgentSession` ni `Room`.

Se reutiliza una sola instancia de `fishaudio.TTS` y se cambia `speed` y `voice_id` con
`update_options()` en vez de reconstruirla por segmento.

### D10. `tests/fixtures/es_30s.wav` se genera, no se graba

El fixture no existia. Se genera con `tests/make_fixture.py` usando Fish TTS
(`s2.1-pro-free`, coste cero) a partir de seis clausulas fijas con numeros, fechas y
nombres propios. Duracion real 34.70 s, 16 kHz mono.

Generarlo en vez de grabarlo hace que cada corrida de F1 sea identica, que es justamente
la razon por la que F1 usa archivo y no microfono.

**Limitacion que hay que tener presente:** el STT corre sobre voz sintetica, que es mas
facil que la voz real. F1 valida la cascada, no la robustez del STT ante voz humana con
ruido de fondo. Eso lo cubre F2 con microfono. Conviene ademas repetir `make dub-file`
con una grabacion real antes de dar por buena la calidad de M3.

### D11. `dub_file.py` y `report.py` como modulos de entrada

La seccion 9 no los lista, igual que no listaba `check.py`. Son los puntos de entrada de
`make dub-file` y `make report`. La orquestacion asincrona real vive en `engine.py`, que
es F2; `dub_file.py` es una cascada secuencial que no la sustituye.

### D13. Directiva para F2: M3 usa el SDK oficial de Deepgram - CUMPLIDA en F2 y F6

Registrada al cerrar F1 para que F2 no repitiera el analisis de D7. **Se cumplio:**
`DeepgramStreamTranscriber` usa `from deepgram import AsyncDeepgramClient`, y M3 no
importa `livekit-plugins-deepgram` en ningun camino. La tercera razon de abajo, la
reconexion con backoff, se implemento en F6 (D32).

Cuando F2 migre M3 a streaming, debe usar el **SDK oficial de Deepgram**, no
`livekit-plugins-deepgram`. Tres razones:

1. El plugin descarta `Word.confidence`, que D1 exige. Su conversion construye
   `TimedString`, subclase de `str` sin campo de confianza, y lo hace en los dos caminos:
   `prerecorded_transcription_to_speech_event` y `live_transcription_to_speech_data`.
   El problema no se arregla solo al pasar a streaming.
2. El camino de migracion a v4 lo aporta el **transporte** de LiveKit, no el envoltorio
   de STT. El motor ya es agnostico del transporte por contrato: cambiar el adaptador no
   obliga a conservar el plugin de STT.
3. F6 exige reconexion de WebSocket con backoff sin perder segmentos (R5). El SDK oficial
   la trae resuelta.

Esto cierra la parte pendiente de D7.

## 2026-08-26 - F2

### D14. El turn-detector corre como runner local, no via `MultilingualModel`

El stack exige endpointing **local y $0**. Dos obstaculos en livekit-agents 1.7.0:

1. `livekit.plugins.turn_detector` esta **deprecado** en favor de
   `livekit.agents.inference.TurnDetector`, que es un **gateway cloud**: exige
   `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` y `LIVEKIT_INFERENCE_URL`. Meteria el RTT
   Arequipa-US (~400 ms) dentro de la etapa que ya domina el TTFA, y anadiria coste.
   Se descarta. Se sigue con el plugin deprecado, que es local.
2. La clase publica `MultilingualModel` exige un `inference_executor` del agent worker:
   `RuntimeError: no job context found`. No usamos agent worker por decision de stack.

Solucion: instanciar `_EUORunnerMultilingual` directamente, llamar `initialize()` una vez
y `run()` por consulta. Es el mismo codigo que el executor despacharia, sin el worker.
Corre en ONNX sobre CPU, 40-110 ms por consulta, y va a un hilo con `asyncio.to_thread`
para no bloquear el bucle mientras el microfono entra.

Riesgo asumido: `_EUORunnerMultilingual` es API privada y el modulo esta deprecado.
Si una actualizacion lo rompe, la alternativa es cargar el ONNX y el tokenizer
directamente; los pesos ya estan en la cache de HuggingFace.

### D15. `adapters/lk_frames.py`: unica frontera con `livekit.rtc`

Silero exige `rtc.AudioFrame` en `push_frame`, y la regla de arquitectura prohibe que
un modulo de M1-M8 importe `livekit.rtc`. La conversion vive en `adapters/`, que es
exactamente la frontera que esa regla define. `gate.py` importa el shim, no rtc. Los
contratos siguen usando `np.ndarray`.

### D16. Semantica de `unlikely_threshold`: no es una compuerta de commit

Error propio, corregido durante F2 y anotado para que no se repita.

El umbral de `languages.json` (es: 0.0058) se uso primero como "si la probabilidad supera
el umbral, cierra el segmento". **Es al reves**: es un *unlikely_threshold*, el valor por
debajo del cual el hablante casi seguro NO ha terminado. Usarlo como compuerta cortaba a
media frase, porque casi cualquier texto supera 0.0058.

Uso correcto, ya implementado: `eou` dispara cuando Deepgram marca `speech_final`
**y** la probabilidad supera el umbral. `speech_final` corrobora, nunca dispara solo.

Se probo ademas usarlo para **alargar** la espera del trigger `timeout` de 0.8 s a 2.5 s
cuando la probabilidad queda por debajo del umbral. Empeoro el TTFA P90 de 3668 a
4291 ms, porque la espera se suma integra a la latencia. **Revertido.** El timeout se
queda en los 800 ms del documento.

### D17. `--from-wav`: banco de pruebas de F2, NO su criterio de aceptacion

El criterio de F2 exige voz real por microfono, 20 turnos y 5 minutos. `live.py` acepta
`--from-wav`, que alimenta el pipeline con un WAV a velocidad de reloj: mismo VAD, mismo
WebSocket, mismos triggers, mismo TTS. Sirve para medir de forma repetible el efecto de
cada ajuste, que es lo que el prompt de F2 pide documentar.

El modo pausa el WAV mientras la puerta esta cerrada, simulando a un hablante que se calla
para escuchar. Sin esa pausa se perdian 320 frames (6.4 s) por sesion y los segmentos
salian fusionados y truncados.

**Limitacion que invalida cualquier conclusion fuerte:** el fixture es voz sintetica con
pausas de 0.6 s entre clausulas (D10). Esas pausas hacen que Deepgram cierre fragmentos a
mitad de frase, lo que dispara el trigger `timeout` y castiga el TTFA. Un hablante humano
no pausa donde pausa este WAV. **El criterio se certifica con microfono, no aqui.**

## 2026-08-26 - F3

### D18. F2 se cierra por decision del usuario, sin la medida por microfono

F2 quedo en EN CURSO: su criterio (TTFA P90 < 2.0 s sobre 20 turnos, cero cortes a media
frase en 5 min de habla real) nunca se midio, porque exige una persona hablando por un
microfono. Las medidas que hay son del banco `--from-wav` (D17), con P90 entre 3301 y
4783 ms segun ajuste.

El usuario decidio darla por cerrada y avanzar a F3. Queda anotado para que el historico
no diga que se midio algo que no se midio. **El criterio de F2 sigue sin verificar** y la
deuda es real: si el TTFA en vivo no cumple, el objetivo es la etapa `speech->stt`, que se
lleva el 67 % del presupuesto.

### D19. El clip de referencia es voz sintetica, igual que el fixture

`tests/fixtures/voz_referencia.wav` (20.31 s) se genero con Fish TTS usando una voz
publica del catalogo, `dfa5b230c8054f429e434f4a6e9bbdec`, distinta de la voz neutra por
defecto del plugin (`933563129e564b19a115bedd57b7406a`, "Sarah", inglesa).

Se eligio una voz **masculina en espanol** a proposito: si el clip de referencia se
generase con la voz neutra, el contraste A/B de F3 seria inaudible y el panel no podria
distinguir nada. Con esta eleccion la diferencia es grande y medible.

Por decision del usuario, las voces reales se enrolan al final del proyecto. Hasta
entonces, F3 valida el **mecanismo** de clonacion, no la calidad sobre voz humana.

### D20. Creacion del modelo de voz por REST, no por el plugin

`livekit-plugins-fishaudio` solo **consume** `voice_id`; no expone creacion de modelos.
`voices.py` llama a `POST https://api.fish.audio/model` con multipart:
`title`, `type=tts`, `train_mode=fast`, `visibility=private`, `voices=<wav>`.
Verificado: HTTP 201, `state: trained`.

No se uso `fish-audio-sdk` (existe, 1.3.0) para no anadir una dependencia mas por una
sola llamada que ya se hace con el `httpx` que el proyecto ya declara.

Tratamiento del dato biometrico, implementado en `FishVoiceRegistry`:

- El `reference_id` se cachea **solo en memoria** del proceso.
- Los bytes del WAV se leen a una variable local y se sueltan; no se guardan en ningun
  atributo ni se copian a disco.
- La lectura va a un hilo (`asyncio.to_thread`): bloquear el bucle congelaria el pipeline.
- `visibility=private`.
- `delete()` y `make enroll-delete ID=` permiten borrar la huella de Fish al terminar.
- El consentimiento se pide **antes** de leer el clip, no despues.

### D21. `SINCRO_VOICE_ID`: reusar un enrolamiento sin resubir el clip

`make enroll` devuelve el `reference_id` y no lo persiste. Para que la sesion en vivo lo
use sin volver a subir la huella, se lee de `SINCRO_VOICE_ID`. Vacio significa voz neutra.

Esto conserva la regla del cache en memoria: lo que se guarda en `.env` es un
identificador opaco de un modelo ya creado en Fish, no el clip ni el audio.

## 2026-08-26 - F4

### D22. `speed_for` recibe un ratio de DURACION, no de bytes

Error propio, encontrado midiendo. El documento define el mecanismo dos como "tras
generar, se compara la **duracion del audio** con la duracion del segmento fuente y se
aplica `speed`". Yo pasaba a `speed_for(seg, budget_ratio)` un ratio de bytes
(`byte_actual / byte_budget`).

Consecuencia medida: como el ingles supera el presupuesto en bytes casi siempre, el ratio
salia >1 y `speed` se iba a 1.10, es decir **aceleraba** un audio que ya era mas corto que
la fuente. Empeoraba la deriva en la direccion equivocada.

Corregido: `DriftController` mantiene una media movil de
`duracion_audio_a_speed_1 / duracion_fuente` (`duration_ratio()`), y eso es lo que se le
pasa. El controlador **no cambia**: sigue siendo byte a byte el del documento.

Efecto: deriva de -103.62 s a **-82.61 s**, y `speed` pasa del rango [0.950, 1.100] a
[0.950, 1.000].

### D23. El criterio de F4 no es alcanzable para ES->EN con el piso de speed en 0.95

**No es un fallo de implementacion: es aritmetica.**

```
habla fuente          : 602.20 s
ratio natural ES->EN  : 0.834   (el ingles dura el 83% de lo que dura el espanol)
piso de speed         : 0.95    (no negociable: protege el timbre clonado)
mejor ratio alcanzable: 0.834 / 0.95 = 0.878
deriva minima posible : (0.878 - 1) x 602.2 = -73.5 s

para |deriva| < 300 ms haria falta speed = 0.834
eso es un 12% por debajo del piso de 0.95
```

Medido: **-82.61 s**. El minimo teorico con el piso vigente es -73.5 s. Es decir, el
controlador esta funcionando cerca de su limite fisico, y ese limite esta a dos ordenes de
magnitud del criterio.

**Lo que si cumple, y es lo que el controlador existe para garantizar:**

```
max atraso: +0.00 s
```

El doblaje **nunca va por detras** en toda la sesion de 10 minutos. El documento define
`drift` como "positivo significa que el doblaje va atrasado", y todo el aparato
(`THRESHOLD_SOFT` acelera, `THRESHOLD_HARD` descarta) esta construido para corregir esa
direccion. La deriva negativa es **holgura**, no desfase: el doblaje termina antes y
espera. En vivo esa holgura la absorbe el silencio entre turnos, no produce desincronia.

**Pregunta abierta para el usuario, R10.** Tres salidas posibles:

1. **Reinterpretar el criterio** como "atraso acumulado < 300 ms cada 10 min". Con esa
   lectura F4 cumple hoy con +0.00 s. Es lo que recomiendo: mide lo que hace dano.
2. **Bajar el piso de speed** de 0.95 a ~0.83. Contradice una regla marcada como no
   negociable en CLAUDE.md y degrada el timbre clonado de forma audible, que es
   justamente lo que F3 acaba de conseguir.
3. **Rellenar con silencio** en vez de estirar el habla. Es lo que hace el doblaje
   profesional. No require tocar `speed` y no degrada el timbre, pero cambia el contrato
   de M7 y es trabajo nuevo, no previsto en el plan de fases.

No elijo por mi cuenta: las tres cambian el contrato o el criterio.

### D24. `should_drop` y `RESET_SILENCE` quedan sin ejercitar en `make drift-test`

El test de 10 min da `drops 0` y `resets 0`, y conviene no confundir eso con "funcionan":

- `should_drop` exige `drift > +2.0 s`. Como la deriva es negativa toda la sesion, nunca
  se cumple la primera condicion.
- `RESET_SILENCE` exige 1.5 s de silencio. El fixture de habla continua tiene pausas de
  0.35 s **a proposito**: si fueran mayores, la deriva se reseteria en cada frase y el
  test no mediria nada.

Ambas rutas se verificaron aparte, con casos directos sobre el controlador, y responden
segun el documento. Queda anotado que el test de 10 min **no** las cubre: en una sesion
real con pausas naturales `RESET_SILENCE` si disparara.

## 2026-08-26 - F5

### D25. El comparador de invariantes tenia un fallo que culpaba al traductor

Error propio, encontrado revisando un resultado que no cuadraba. La primera corrida de la
matriz dio `es -> ja` con 13/19 invariantes y `ja -> *` con solo 4 de 19 "supervivientes
al STT de la fuente". Parecia confirmar R1.

**No era el modelo: era mi instrumento de medida.** El japones escribe los numeros en
kanji y los nombres propios en katakana, y `find_invariants` comparaba cadenas ASCII:

```
esperaba "2027"   el texto decia  二千二十七
esperaba "48291"  el texto decia  四万八千二百九十一
esperaba "5400"   el texto decia  五千四百
esperaba "Lima"   el texto decia  リマ
esperaba "Cusco"  el texto decia  クスコ
```

La traduccion era correcta en todos esos casos. Corregido: `matrix.py` ahora normaliza con
NFKC (digitos de ancho completo a ASCII), convierte numerales kanji a arabigos con un
parser propio (`_kanji_to_int`, que maneja 十/百/千/万/億) y acepta transliteraciones en
katakana de los nombres propios. Sobre el mismo texto japones pasa de 13/19 a **19/19**.

La leccion queda anotada porque es la trampa de la fase: **medir mal un idioma que no
lees produce un falso positivo de riesgo**, y habria llevado a cambiar de modelo sin
motivo.

### D26. Los factores de expansion medidos viven en `out/expansion.json`, no en el codigo

`config.expansion_for` resuelve con esta prioridad: **medido en F5 > tabulado en el
documento > derivado de verbosidad**. Los valores medidos se escriben con
`make matrix-test ARGS=--apply` y se leen de `out/expansion.json`.

Se hizo asi y no cableando numeros en `config.py` porque la recalibracion depende del
modelo de traduccion y del modelo de TTS: cambiar cualquiera de los dos invalida los
factores, y con un archivo generado se vuelve a medir en vez de editar codigo a mano. El
archivo registra la fecha y el `llm_model` con el que se midio.

Los 8 pares con japones no aparecen: no usan presupuesto de bytes.

### D27. Voces nativas por idioma en los fixtures de la matriz

Los primeros fixtures se generaron con la voz por defecto de Fish, que es inglesa. El
acento degradaba el STT del propio fixture y contaminaba la medida antes de traducir nada:

```
pt-BR  "eu trabalho de Arequipa"  ->  "é o trabalho diário equipa"   y  12 -> 2
ja     "2026年"                    ->  "二千アーシ留年"
ja     "リマのチーム"                ->  "今のチーム"
```

`tests/matrix_scripts.py` fija ahora una voz nativa por idioma (`SOURCE_VOICES`). Con eso
el fixture japones transcribe practicamente perfecto. Queda un resto conocido: `Arequipa`
se pierde o se deforma en el STT de en/pt/fr, que es R9 actuando en la direccion fuente.
Por eso el conteo de invariantes solo penaliza los que **si** sobrevivieron al STT de la
fuente: perder algo que nunca llego a la traduccion no es culpa del traductor.

### D28. `committer.py` unia las palabras japonesas con espacios

Bug real del motor, no del instrumento de medida, encontrado al investigar por que el
japones fuente puntuaba 6/19.

Deepgram devuelve el japones en morfemas sueltos. `_emit` hacia
`" ".join(w.text for w in words)`, con lo que el texto del segmento salia asi:

```
"20 2 6 年 の 第 3 四半 期"        en vez de  "2026年の第3四半期"
"ロ ドリ ゴ"                      en vez de  "ロドリゴ"
"ア レ キ パ"                     en vez de  "アレキパ"
```

**Ese texto roto es el que se le enviaba al traductor**, no solo al comparador. Afecta a
los cuatro pares `ja -> *` y no se habria visto sin el guion de invariantes: el modelo
traducia razonablemente a pesar del destrozo, asi que la salida "parecia bien".

Corregido con `join_words(words, lang)` y `NO_SPACE_LANGS = {"ja"}`, aplicado a
`PunctuationCommitter` y a `StreamingCommitter`.

### D29. `normalize()` borraba el dakuten japones

Segundo fallo del comparador. Descomponia en NFKD y tiraba **todos** los signos
combinantes para quitar acentos latinos, lo que en japones convierte `ロドリゴ` en
`ロトリコ` y `ディエゴ` en `ティエコ`: el dakuten no es un acento decorativo, distingue
consonantes sordas de sonoras.

Corregido: solo se descarta el signo combinante cuando el caracter base es latino
(`ord <= 0x024F`). Los acentos de `Á É Ñ ç` se siguen quitando; el dakuten se conserva.

### D30. La cuota diaria de Groq limita cuantas veces se puede correr la matriz

Al relanzar `make matrix-test` con los arreglos D28 y D29 la corrida murio a mitad:

```
Error code: 429 - Rate limit reached for model `qwen/qwen3.6-27b`
service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199761
```

**200.000 tokens por dia en el tier gratuito.** Una pasada completa de la matriz consume
del orden de 55.000 tokens de entrada, y `make drift-test` otros 26.000. Entre las tres
corridas de matriz de hoy mas la de deriva se agoto el dia.

Consecuencias practicas:

- La matriz no se puede iterar mas de dos o tres veces al dia. Cualquier ajuste que
  obligue a remedirla cuesta una jornada.
- F6 (`make soak MIN=20`) tambien consume tokens y habra que planificarlo.
- El presupuesto del proyecto (menos de \$20 en total) sigue siendo correcto en dinero;
  el limite que muerde es el de **tokens por dia**, no el de coste.

Mitigacion inmediata: `make matrix-test ONLY=<idioma>` corre solo una fuente (4 pares) y
cuesta una quinta parte. Para cerrar F5 basta con volver a medir las fuentes afectadas.

## 2026-08-26 - F6

### D31. El corte de red se simula en el WebSocket, no en la red del sistema

`make soak` no toca iptables ni desconecta la interfaz. `simulate_network_drop(segundos)`
cierra el socket de Deepgram y bloquea la reconexion durante ese tiempo, que es
exactamente lo que el motor observa cuando la red cae. Tres razones:

1. **No necesita root.** Un test que exige privilegios no se corre.
2. **Es reproducible.** El corte cae en el segundo exacto que se pide, siempre.
3. **Apunta al riesgo correcto.** R5 es "caida del WebSocket de Deepgram en sesion larga",
   no "caida de toda la red". Cortar la interfaz mataria ademas Groq y Fish, y mezclaria
   tres fallos en una sola medida.

Limitacion aceptada: no ejercita la reconexion de TCP ni de DNS. Un corte real de
interfaz podria comportarse distinto en esos niveles.

### D32. Reconexion: buffer acotado mas ventana de reenvio

El criterio pide recuperarse "sin perder segmentos", y para eso no basta con reconectar.
Tres piezas en `DeepgramStreamTranscriber.stream`:

- **Colector siempre activo.** Consume `frames` tambien mientras el socket esta caido. Si
  se parase, el microfono se atascaria y se perderia audio en origen.
- **Buffer acotado a 30 s.** Guarda lo capturado durante el corte. Si el corte se alarga,
  descarta lo mas viejo y lo cuenta en `frames_dropped_overflow`: en tiempo real el audio
  viejo ya no sirve para doblar, y crecer sin limite acabaria en OOM en una sesion larga.
- **Ventana de reenvio de 10 s.** Guarda el audio posterior al ultimo `is_final` recibido.
  Deepgram no confirma que proceso, asi que el segmento a medias se perderia al caer el
  socket. Al reconectar se reinyecta esa ventana antes que el audio nuevo, y se vacia en
  cuanto llega un `is_final`.

**Continuidad de la linea temporal.** Deepgram reinicia sus timestamps de palabra en cada
socket nuevo. Sin corregirlo, tras reconectar los tiempos volverian a cero y el TTFA
saldria absurdo. Se lleva `_sent_total` acumulado entre conexiones y `_conn_offset` con el
valor al abrir cada una; los timestamps de palabra se desplazan por ese offset.

### D33. Degradacion del TTS a subtitulo

`engine._process` captura `SynthesisError` y sigue. El turno queda con `audio_duration=0`,
se marca `tts_failed`, y `live.py` lo imprime como `[SUBTITULO: TTS caido]` con la
traduccion en pantalla.

Dos detalles que importan:

- El turno degradado **no alimenta la calibracion de duracion** del `DriftController`: un
  audio de cero segundos falsearia el ratio y arrastraria el `speed` de los turnos
  siguientes.
- Un fallo de **traduccion** es distinto: sin texto no hay subtitulo que ensenar. El bucle
  de salida lo registra y continua con el turno siguiente, sin tumbar la sesion.

### D34. `--offline-llm`: prueba de resistencia sin cuota de Groq

La cuota diaria (D30) se agoto otra vez a mitad de F6. `make soak ARGS=--offline-llm`
sustituye M5 por el `FakeTranslator` determinista y deja el resto del pipeline intacto:
mismo VAD, mismo WebSocket con su reconexion, mismos triggers, mismo TTS real.

Mide lo que el criterio de F6 pide de verdad —**estabilidad durante 20 minutos sin
intervencion y recuperacion automatica del corte**— sin gastar tokens. **No mide calidad
de traduccion**, que se verifica aparte con el traductor real en una corrida corta.

Efecto secundario util: el `FakeTranslator` invierte el texto, y Fish lo pronuncia mas
despacio que a una frase real, asi que la deriva se va **positiva**. Es la primera vez que
se ejercita esa direccion del `DriftController`; el test de F4 solo produjo deriva
negativa y dejo `should_drop` sin disparar (D24).

### D35. El TTFA puede salir ligeramente negativo con el traductor falso

En el soak con `--offline-llm` aparecen valores como `ttfa_ms: -84`.

No es un error de signo ni un turno que suene antes de hablarse. `t_speech_end` sale del
timestamp de la ultima palabra que da Deepgram, traducido a tiempo de captura; el margen
de ese mapeo es de algunas decenas de milisegundos. Con el traductor real, el LLM anade
unos 700 ms y el margen queda enterrado. Con el traductor falso la cascada es casi
instantanea y el error de mapeo aflora con signo negativo.

Es el limite de resolucion de la medida, y conviene tenerlo presente al leer percentiles
de TTFA muy bajos. No se corrige con un `max(0, ...)`: eso ocultaria el margen en vez de
mostrarlo.

## 2026-08-26 - Ajuste de latencia, post-F6

### D36. `TIMEOUT_S` de M4 baja de 800 a 400 ms

Cambio medido, no supuesto. Dos corridas de 210 s por configuracion sobre
`es_10min.wav`, ~45 turnos cada una:

| `TIMEOUT_S` | P50 | P90 |
|---|---|---|
| 0.800 | 3564 / 3668 ms | 4416 / 5087 ms |
| **0.400** | **2918 / 2866 ms** | **4132 / 4062 ms** |

Media de las dos corridas: **P50 −726 ms, P90 −655 ms**.

**Sin coste en calidad.** Se comparo el texto de los segmentos con las dos
configuraciones sobre el mismo audio y las fronteras salen **identicas**. Los cortes a
media frase que se ven no los causa el timeout: los causa `eou` (ver abajo).

El documento tecnico fija 800 ms en la seccion 5. Es un desvio consciente respaldado por
medida; si la seccion 5 gana, se revierte con una constante.

### D37. Los cortes a media frase vienen de `eou`, no del timeout

Hallazgo del mismo experimento, y corrige una atribucion mia equivocada. Con el mismo
audio y las dos configuraciones de timeout, estos cortes aparecen **igual**:

```
"...revisar el presupuesto de operaciones"  |  "antes del 4 de enero de 2027..."
"...atendimos 16 solicitudes nuevas"        |  "y resolvimos casi todas el mismo dia."
"...el equipo de Trujillo supero"           |  "su objetivo mensual."
"...para abril depende de que"              |  "cerremos 21 acuerdos mas."
```

Los dispara el turn-detector: son fragmentos **gramaticalmente completos** que el hablante
continua. `"Necesitamos revisar el presupuesto de operaciones"` es una oracion valida, el
modelo la puntua como fin de turno, y la subordinada que venia detras se parte.

Las guardas de no-corte no pueden atraparlo: miran si el fragmento **termina** en
conjuncion, preposicion o articulo, y `"operaciones"` es un final legitimo. La senal que
faltaria esta en la palabra **siguiente**, que aun no existe cuando hay que decidir.

**Es el proximo objetivo de latencia y de calidad a la vez.** Palancas posibles, ninguna
medida todavia:

1. Subir el umbral de `eou` por encima del `unlikely_threshold` de `languages.json`
   (es: 0.0058). Menos cortes, mas espera.
2. Exigir `speech_final` **ademas** de `eou` para cerrar. Hoy `speech_final` corrobora
   pero `eou` puede disparar sin el.
3. Dar al turn-detector mas contexto: hoy se le pasa solo el fragmento pendiente, no los
   turnos anteriores, y `predict_end_of_turn` acepta un `ChatContext` completo.

### D38. Bajar el `endpointing` de Deepgram NO sirve: medido y descartado

Una primera medida con n=8 sugeria que bajar de 300 a 200 ms ahorraba 224 ms. **Con n=40
el resultado se invierte:**

```
endpointing=300  p50  851ms  p90 1722ms  sd 447
endpointing=200  p50  766ms  p90 2338ms  sd 628
endpointing=150  p50  806ms  p90 2285ms  sd 665
```

La mediana mejora 95 ms pero el **P90 empeora 616 ms** y la dispersion crece un 40 %.
Confirmado end-to-end: P90 de 4662 a 6054 ms. Con menos silencio exigido Deepgram cierra
fragmentos antes, produce mas por frase, y el committer tiene que acumular mas.

Como el criterio de F2 es P90, el cambio es una perdida neta. **Revertido a 300.**

La leccion, que costo dos medidas contradictorias: con n=8 cualquier diferencia de
percentil es ruido.

### D39. Los 150-300 ms que anuncia Deepgram no son el tiempo hasta `is_final`

Confusion que conviene dejar escrita porque invita a buscar el problema donde no esta.

Deepgram anuncia latencia de **procesado**: de recibir un chunk a emitir transcripcion
para ese chunk. Eso se cumple, y se ve en los parciales. Pero `is_final` exige ademas que
Deepgram **observe `endpointing` ms de silencio despues de la ultima palabra**, y esa
espera se suma por definicion.

Medido desde el fin de la ultima palabra hasta que llega `is_final`, n=40:
**p50 851 ms, p90 1722 ms**, con minimo de 370 ms.

Descomposicion aproximada: ~300 ms de endpointing + ~200 ms de procesado + ~180 ms de red
(Deepgram esta a 177 ms de TCP desde Arequipa) + jitter.

**El gate de VAD no tiene la culpa.** Se sospecho que recortar el silencio dejaba a
Deepgram sin material para endpointar. Medido: 1009 ms con gate contra 1023 ms sin el.
Sin diferencia; el hangover de 160 ms le basta.

## 2026-08-29 - Inicializacion del entorno de desarrollo

### D40. `pip-system-certs` anadido a las dependencias, solo Windows

`make check` fallaba en un entorno Windows con las 3 credenciales `unreachable:
ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get
local issuer certificate`, mientras `curl` a los mismos hosts funcionaba. Diagnosticado
con `openssl s_client`: el certificado que llega al proceso Python esta emitido por
`Avast Web/Mail Shield Root`, la CA que Avast Antivirus inyecta para inspeccionar TLS.
Windows confia en esa CA (por eso `curl` y el navegador no fallan); el bundle de
`certifi` que usan `httpx`/`livekit` no la incluye y nunca la incluira, porque es local a
esta maquina.

No es un problema del proyecto ni de una API: es un antivirus haciendo TLS interception
en la maquina de desarrollo. `pip-system-certs` reemplaza la verificacion basada en
`certifi` por el almacen de certificados del sistema operativo (Windows Certificate
Store), donde la CA de Avast ya esta confiada. Se instala solo como `.pth` hook al
importar `ssl`, sin tocar codigo del proyecto.

Acotado a `sys_platform == 'win32'`: en Linux/macOS, donde corre CI y donde no hay este
antivirus, `certifi` sigue siendo la fuente de confianza.

Verificado:

```
antes: httpx.get('https://api.deepgram.com') -> SSL: CERTIFICATE_VERIFY_FAILED
despues (con pip-system-certs instalado): httpx.get(...) -> 404  (TLS ok, ruta no existe)
make check -> 3/3 credenciales validas
```

### D41. `deepgram-sdk` faltaba en `pyproject.toml`: `make live` habria fallado en runtime

D13 exige que `transcriber.py` use el SDK oficial de Deepgram (`from deepgram import
AsyncDeepgramClient`) para el streaming de F2, no el plugin de LiveKit. La evidencia de
F2 y F6 en `STATE.md` da por corrido ese camino, pero `pyproject.toml` solo declaraba
`livekit-plugins-deepgram`: el paquete `deepgram-sdk` nunca se anadio a las dependencias
del proyecto.

`mypy --strict` lo delato al inicializar el entorno desde cero:

```
src\sincro\transcriber.py:299: error: Cannot find implementation or library stub for
module named "deepgram"  [import-not-found]
```

En cualquier `.venv` nuevo instalado solo desde `pyproject.toml`, `make live` habria
fallado con `ModuleNotFoundError: No module named 'deepgram'` en el primer turno de
streaming. No se detecto antes porque los `.venv` usados en F2-F6 ya tenian el paquete
instalado a mano, sin registrarlo en el manifiesto.

Anadido `deepgram-sdk>=7.0` a las dependencias. Verificado: `mypy` vuelve a pasar sin
errores sobre los mismos archivos.

### D42. `make live` exige descargar los pesos del turn-detector a mano antes del primer uso

El README decia "la primera ejecucion descarga los pesos... sola". Es falso: el plugin
`livekit-plugins-turn-detector` carga el modelo ONNX con `local_files_only=True`
(`base.py`, `initialize()`), asi que si los pesos no estan ya en la cache de HuggingFace,
`gate.py:load_eou()` revienta con `RuntimeError: ... Could not find file
"model_q8.onnx"` en el primer `make live` de una maquina nueva. No es un bug del
proyecto: es el comportamiento del plugin de terceros, mal documentado en el README.

Corregido de dos formas:

1. `Makefile`: `setup` ahora corre `$(PY) -m livekit.agents download-files` despues de
   instalar dependencias, asi que una instalacion nueva vía `make setup` ya lo resuelve.
2. `README.md`, seccion de `make live`: la nota pasa de "se descarga sola" a el comando
   explicito, para quien instalo con la Opcion B (sin `make`) o con un `.venv` mas viejo
   que el cambio al Makefile.

Verificado en Windows, entorno recien inicializado:

```
$ python -m livekit.agents download-files
descarga deepgram, fishaudio, openai, silero, turn_detector (v1.2.2-en y v0.4.1-intl)
finished downloading files for livekit.plugins.turn_detector

$ python -m sincro.live
  Llevas auriculares puestos? [s/N] s
  cargando turn-detector (local, primera vez tarda)...
  <ya no revienta, sigue a abrir el microfono>
```

## 2026-08-29 - v4, G0

### D43. R8 aplicado: `openai/gpt-oss-120b` con `reasoning_effort=low`, pero el gate de tokens de D8 NO se cumple

R8 pedia migrar de `qwen/qwen3.6-27b` (tier Preview en Groq, no apto para produccion) a
`gpt-oss-120b` (tier Production), verificando que el gate de D8 (<100 tokens de salida
por clausula) siguiera en verde. **No sigue en verde**, y el motivo es mas grave que el
costo: bajo presupuesto de bytes real, el segmento puede salir vacio.

Primero, `reasoning_effort=none` **no es valido** para este modelo en Groq:

```
400 - `reasoning_effort` must be one of `low`, `medium`, or `high`
```

Con `reasoning_effort=low` y `SINCRO_LLM_MAX_TOKENS=200` (el valor heredado de qwen),
`make dub-file` sobre `tests/fixtures/es_30s.wav` dio **142.8 tokens de salida por
clausula** (objetivo <100) y **un segmento completamente vacio** (seg 2, `tokens_out=200`
exacto, texto `''`): el motor lo trato como si no se hubiera dicho nada, cero audio para
ese turno. No es ruido de una corrida: aislado y repetido con un script directo contra
`GroqTranslator.translate()`, es 100% reproducible.

Causa raiz, aislada variando el presupuesto de bytes del mismo segmento:

```
budget=999 (sin restriccion real): traduce bien, 32 tokens de salida
budget=82  (el real, calculado)  : texto vacio, 200 tokens de salida (tope alcanzado)
```

Y variando `max_tokens` con el presupuesto ajustado (budget=82):

```
max_tokens=200 : vacio (corta a medias)
max_tokens=400 : vacio (sigue cortando)
max_tokens=800 : traduce bien -- pero 386 tokens de salida
max_tokens=1500: traduce bien -- 419 tokens de salida
```

**El modelo, con `reasoning_effort=low`, gasta 300-400+ tokens de razonamiento invisible
intentando resolver la restriccion de bytes del prompt antes de escribir la traduccion
visible.** Con `max_tokens` insuficiente para terminar ese razonamiento, la respuesta
visible sale vacia: una falla de correctitud silenciosa, no solo de costo o latencia.
`reasoning_effort=medium` tiene el mismo comportamiento (probado, mismo resultado vacio
con budget=82 y max_tokens=200).

La nota de R8 en `STATE.md` ("gpt-oss-120b low... ya paso el gate de tokens, 28 de
salida") se midio sin la restriccion real de bytes en el prompt: con budget suelto este
mismo segmento tambien da un resultado bajo (32 tokens). El gate solo se rompe con
presupuesto ajustado, que es la condicion real de produccion.

**Decision del usuario: subir `SINCRO_LLM_MAX_TOKENS` a 800** en vez de revertir a qwen o
seguir buscando otro modelo. Consecuencias aceptadas conscientemente, no un exito
disfrazado:

```
make dub-file, mismo fixture, max_tokens=800:
  seg 1: out=47   seg 2: out=411   seg 3: out=86
  seg 4: out=83   seg 5: out=52    seg 6: out=82
  promedio: 126.8 tokens/clausula   (objetivo <100: NO CUMPLE)
  0 segmentos vacios, 0 fugas de marcadores, 6/6 traducciones correctas
```

El gate de D8 queda roto como excepcion documentada, no oculta. Highly variable: un
segmento puede salir en 47 tokens y el de al lado en 411, segun cuanto "piense" el modelo
sobre el presupuesto -- imprevisible por diseño de este modelo con razonamiento activado.

**Lo que SI mejora, pese al gate roto:** el costo en dolares baja igual, porque el precio
por token de gpt-oss-120b es mucho menor que el de qwen3.6-27b:

```
qwen3.6-27b     (evidencia F1 original): $0.001053 total, mismo fixture
gpt-oss-120b/low (esta corrida)        : $0.000689 total, mismo fixture
```

**Lo que empeora:** el tramo LLM del TTFA. Mas tokens de salida es mas tiempo de
generacion en el camino critico, y ahora es impredecible (47 a 411 tokens en el mismo
fixture) en vez de estable como con qwen (15-18 tokens siempre). Esto se mide en G0 al
comparar el TTFA por etapa contra la telemetria de F2.

`.env` y `.env.example` quedan con `SINCRO_LLM_MODEL=openai/gpt-oss-120b`,
`SINCRO_LLM_REASONING_EFFORT=low`, `SINCRO_LLM_MAX_TOKENS=800`, con un comentario que
remite a esta decision.

### D44. `adapters/ws_io.py`: cuatro decisiones que el contrato dejaba abiertas

El contrato de Notion especifica el formato binario exacto, pero deja cuatro cosas sin
resolver que hubo que decidir para implementar A1:

**1. `flags.fin` no tiene ningun evento equivalente en el nucleo.** `DubbingEngine`
expone `on_audio(chunk)` por cada trozo de audio de Fish, pero ningun callback avisa
"este era el ultimo trozo del segmento" — `on_audio` no lo distingue. Tocar `engine.py`
para anadir esa senal violaria "no toques el nucleo". Solucion: el motor SI expone
`on_turn(TurnResult)`, que dispara una vez por segmento terminado, con o sin audio.
`WebSocketAudioSink.end_utterance()` se cuelga de ese callback (no de `on_audio`) para
rellenar el ultimo frame parcial con silencio y marcarlo `fin=1`. Es composicion en
`ws_serve.py`, cero cambios en `engine.py`.

**2. TTS a 16 kHz, no a 44100 Hz.** F2/F3 sintetizaban a 44100 Hz porque el destino era
un altavoz local. El contrato exige 16 kHz mono en las dos direcciones del socket.
`FishSynthesizer` ya acepta `sample_rate` como parametro (D9); en `ws_serve.py` se le
pasa 16000 directamente. Cero resampleo, cero latencia anadida, cero codigo nuevo en
M7 — es un valor de configuracion en la capa de composicion, no un cambio de contrato.

**3. `TCP_NODELAY` no tiene parametro en `websockets.serve()`.** La libreria (v15, API
`websockets.asyncio.*`) no expone un flag para esto. `connection.transport` es el
`asyncio.Transport` real, guardado en `connection_made()` y ya usado por la propia
libreria para `local_address`/`remote_address`; no es publico en `dir()` pero es estable.
`enable_tcp_nodelay()` en `ws_io.py` hace
`connection.transport.get_extra_info("socket").setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)`
sobre cada conexion aceptada. Precedente: D14 ya entro a una clase interna de un plugin
cuando la API publica no alcanzaba.

**4. Backpressure de salida: cola acotada a 25 frames (~500 ms), descarte del mas
viejo.** CLAUDE.md exige "descarte, nunca cola" pero no da un numero. 25 frames absorbe
jitter normal de red sin acumular medio segundo de retardo audible si el cliente de
verdad se atrasa. `frames_dropped` se cuenta y se loguea (nunca es un descarte
silencioso). El limite es ajustable; no hay medicion todavia de cual es el optimo real
contra un cliente movil.

### D45. `make ws-test` corre servidor y cliente en el mismo proceso, no dos comandos

El criterio de G0 (`make ws-test`) necesita un servidor WebSocket y un cliente
simultaneos. En vez de dos targets de Makefile con un proceso en segundo plano (fragil
en Windows sin `make` real, ver D40), `src/sincro/ws_test.py` levanta `ws_serve.run_server`
como tarea de asyncio, corre el envio/recepcion del fixture en el mismo loop, y cierra
el servidor al terminar. `tests/ws_client.py` se mantiene como herramienta aparte,
reusable contra un servidor real en G3/G4, con su propia logica de envio/recepcion (algo
de codigo duplicado con `ws_test.py`, pero evita que `src/sincro/` dependa de `tests/`).

### D46. G0 verificado: el adaptador de WebSocket no anade latencia frente a F2

`make ws-test` sobre `tests/fixtures/es_30s.wav`, comparado etapa por etapa contra un
`make live --from-wav` recien corrido con la misma configuracion (mismo modelo post-D43,
para que la unica variable sea el transporte):

```
etapa          F2 (console_io)   ws-test (ws_io)
speech->stt        2947 ms           1065 ms
stt->llm1           944 ms           1011 ms
llm1->done           40 ms             37 ms
done->tts1         2263 ms            630 ms
tts1->out             0 ms              0 ms
TOTAL (prom.)      6194 ms           2742 ms

TTFA  F2: P50 6418  P90 8894  P99 9195 ms  (6 segmentos)
      ws: P50 2496  P90 3708  P99 3854 ms  (5 segmentos)
```

Ninguna etapa empeora. `stt->llm1` sube 67 ms, dentro de la varianza normal de Groq ya
documentada (D8, D38). El resto mejora, sobre todo `done->tts1`, que es varianza de Fish
(ya caracterizada esta misma sesion: 300 ms a 7000 ms segun el momento, independiente del
transporte).

**Salvedad honesta sobre los 5 segmentos contra 6:** `tests/ws_client.py` no pausa el
envio del WAV mientras el gate del servidor esta mudo — a proposito, porque un llamante
real no tiene forma de saber que el otro extremo esta reproduciendo audio. El banco de
F2 (`paced_wav()` en `live.py`) si pausa, porque ahi el "hablante" es un archivo y pausar
evita que el WAV se superponga con su propio doblaje. Esa diferencia de metodologia
cambia donde el committer corta las frases (4 `eou` + 1 `timeout` contra 3 `eou` + 2
`timeout` + 1 `eou`), no la velocidad del adaptador. No se oculta: se anota.

No se implemento CallSession ni M9 (G1), ni la API de control (G2), tal como se pidio.

## 2026-08-29 - v4, G1

### D47. `EchoGate` (M9) es subclase de `SileroGate`, no envoltorio -- y por que

`DubbingEngine.__init__` tipa su parametro `gate` como `SileroGate` concreto, no como
el Protocol `AudioGate` de `contracts.py` (que si bastaria estructuralmente). Un
envoltorio de composicion (una clase nueva que solo *tuviera* un SileroGate adentro)
habria chocado con ese tipo bajo `mypy --strict`, y arreglarlo bien --ensanchar el tipo
a `AudioGate` en `engine.py`, y anadir `unmute_after_guard` al Protocol y al Fake en
`contracts.py`/`fakes.py`-- son tres archivos del nucleo. Se penso en pausar y
preguntar; en vez de eso se encontro una salida que no toca ningun archivo del nucleo:

`EchoGate` **hereda** de `SileroGate`. `isinstance(echo_gate, SileroGate)` es cierto de
verdad, `mypy --strict` lo acepta sin ignore, y `process()`/`load_eou()`/
`eou_threshold`/`stats` funcionan tal cual estan en `gate.py` porque son heredados, no
reimplementados. Solo se sobreescriben `mute()`, `unmute()` y `unmute_after_guard()`.

Redirigirlos con `self.target.mute()` no sirve: `target` es OTRO `EchoGate`, cuyo
`mute()` tambien esta redirigido -- A muta a B, B muta a A, A muta a B... recursion
infinita, verificada al escribirlo (la primera version colgaba `make call-test` sin
error, solo se agotaba el stack). La solucion es llamar al metodo **sin ligar** de
`SileroGate` directamente sobre `target`: `SileroGate.mute(self.target)` ejecuta el
"mutate a ti mismo" real -- pone `target.muted = True` en el propio `target` -- sin
volver a pasar por el `mute()` sobreescrito de `target`. Es el mismo patron que D14 usa
para llamar directo a una clase interna de un plugin cuando la API publica no alcanza,
aplicado esta vez a codigo propio en vez de a una libreria de terceros.

**Politica de interbloqueo** (pedida explicitamente en la tarea):

- El estado de mute es por DIRECCION: cada `EchoGate` tiene su propio `muted`/
  `mute_calls` heredados; no existe un mute global de la sesion.
- `mute()`/`unmute()` son escrituras de atributo, no locks. Si los dos motores emiten a
  la vez, cada uno escribe el `muted` del gate CONTRARIO sin esperar nada del otro lado:
  no hay espera circular posible, no hay deadlock en el sentido de bloqueo mutuo.
- Riesgo real, heredado sin cambios de `gate.py` (no arreglable sin tocar el nucleo):
  `unmute_after_guard()` llama a `unmute()` de forma incondicional tras dormir 150 ms,
  sin comprobar si un segundo segmento ya volvio a mutear el mismo gate mientras
  dormia. Si un motor emite dos segmentos seguidos con menos de 150 ms entre el fin del
  primero y el mute del segundo, el guard del primero puede desmutear a mitad de la
  reproduccion del segundo. Ya existia en v3 (mismo mecanismo, self-mute); v4 solo lo
  reubica de "un motor se desmutea de mas pronto" a "el gate del otro participante se
  desmutea de mas pronto". No se disparo en `make call-test` (mute_calls==unmute_calls
  limpio en las dos direcciones), pero no esta descartado para segmentos mas cortos y
  seguidos que los de las corridas de prueba.

### D48. Bug real: el `state` de una direccion se quedaba pegado en "speaking"

Primera version de `_DirectionState.on_segment_committed`/`on_turn` en
`call_session.py` mandaba `translating`/`idle` solo `if not gate.is_speaking`. Medido
en `make call-test`: un lado de la llamada mostraba una secuencia rica
(`speaking, translating, idle, translating, idle`) y el otro se quedaba en
`['speaking']` para siempre, sin importar cuantos turnos completara. No era aleatorio
ni dependia de que idioma fuera: dependia de que participante conectaba primero.

Causa: `gate.is_speaking` (heredado de SileroGate, `_speaking` del watcher VAD) exige
`min_silence_duration` (550 ms por defecto) de silencio sostenido para bajar a `False`.
El committer puede cerrar un segmento por otras senales (`eou`, `punctuation`,
`timeout`) **antes** de que ese silencio se cumpla. Resultado: en el momento en que
`on_segment_committed`/`on_turn` se ejecutan, `gate.is_speaking` casi siempre sigue en
`True` todavia -- la condicion de guarda estaba, en la practica, siempre cerrada. Un
lado "se salvaba" por azar de temporizacion (pausas mas largas entre frases en un
fixture que en el otro); el otro nunca.

Corregido quitando la guarda: `on_segment_committed` manda `translating` y `on_turn`
manda `idle` sin condicion. El commit YA es la senal autoritativa de fin de turno; no
hace falta corroborarla con una senal de VAD que se demora mas en bajar. Reverificado:
las dos direcciones muestran secuencias largas y variadas de `state`, sin repetidos
seguidos ni `idle` antes de cualquier `speaking`.

### D49. `make call-test` necesita WAV de duracion parecida, o un lado "cuelga" primero

Primera corrida uso `tests/fixtures/matrix_en.wav` (41.87 s, 10 frases) contra
`tests/fixtures/es_30s.wav` (35.16 s, 6 frases) con el mismo `--tail-s` para los dos
clientes. El participante mas corto cierra su conexion antes; `CallSession.run()` usa
`asyncio.wait(..., FIRST_COMPLETED)` y cancela la direccion sobreviviente en cuanto la
otra termina -- exactamente lo que el contrato pide ("un lado cuelga, la sesion
termina, `peer_left` al otro"), pero corta la medida del lado mas largo a la mitad
(4 turnos en vez de los ~10 esperados, y su canal de `state` se corta con el).

No es un bug de `CallSession`: es un requisito del arnes de prueba. Se genero
`tests/fixtures/matrix_en_35s.wav` (recorte a 35.0 s del `matrix_en.wav` existente, sin
gastar cuota de Fish) para igualar duraciones. `WAV_EN`/`WAV_ES` en el Makefile quedan
configurables; quien use `make call-test` con fixtures propios debe igualar la duracion
o aceptar que el mas corto termina la llamada primero.

## 2026-08-29 - v4, correccion de call_serve.py antes de abrir G2

### ACLARACION. Dispatcher y Worker son dos servicios, no uno -- `call_serve.py` esta bien como esta

Al leer la tarea de G2 crei que `call_serve.py` (`websockets.serve()` sin HTTP) tenia
que crecer para servir tambien `POST /v1/sessions`, `DELETE /v1/sessions/{id}` y
`POST /v1/voices`. Investigue si eso era viable con la libreria `websockets` y **no lo
es**: su parser HTTP/1.1 rechaza cualquier metodo que no sea GET antes de que el hook
`process_request` pueda intervenir (verificado en el codigo fuente, no supuesto):

```
.venv/Lib/site-packages/websockets/http11.py:150-151
    if method != b"GET":
        raise ValueError(f"unsupported HTTP method; expected GET; got {d(method)}")
```

El usuario aclaro que esto no bloqueaba nada: el contrato de Notion describe **dos
servicios** en Container Apps separadas, no uno:

- **Dispatcher** (FastAPI, puerto 8080 propio): `POST /v1/sessions`,
  `DELETE /v1/sessions/{id}`, `POST /v1/voices`, `GET /v1/voices/{user_id}`, OpenAPI.
- **Worker** (`websockets.serve()`, puerto 8080 propio, otra Container App):
  `WS /v1/stream`, `GET /healthz`, `GET /readyz` -- las dos ultimas son GET, van por
  `process_request` sin problema.

El documento de contrato lista los endpoints en una sola tabla (seccion 4) sin separar
por servicio explicitamente, pero el propio ejemplo de `POST /v1/sessions` ya daba por
supuesta la separacion: devuelve un `ws_url` que apunta a `worker-1.sincro....`, un
FQDN distinto al del dispatcher. **`call_serve.py` no necesita ningun cambio**: ya es
el Worker completo. El Dispatcher (S1, FastAPI, S2 VoiceRepository) se construye aparte
en G2 sin tocar este archivo. Se registra como ACLARACION porque no cambia ni el
contrato ni el codigo -- corrige una lectura mia de la tabla, no una decision de diseno.

### D50. `TCP_NODELAY` ya esta activo por defecto -- verificado en el codigo fuente de asyncio, no en `websockets`

`call_serve.py` no llama a `enable_tcp_nodelay()` en ningun lado, a diferencia de
`CallSession.__init__` (que si lo hace sobre las dos conexiones antes de construir los
motores). Verificado si hacia falta anadirlo tambien aqui, en vez de asumir que no:

```
C:\...\Python313\Lib\asyncio\base_events.py:192-197
    if hasattr(socket, 'TCP_NODELAY'):
        def _set_nodelay(sock):
            if (sock.family in {socket.AF_INET, socket.AF_INET6} and
                    sock.type == socket.SOCK_STREAM and
                    sock.proto == socket.IPPROTO_TCP):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

C:\...\Python313\Lib\asyncio\selector_events.py:942 (_SelectorSocketTransport.__init__)
    base_events._set_nodelay(self._sock)

C:\...\Python313\Lib\asyncio\proactor_events.py:611 (_ProactorSocketTransport)
    base_events._set_nodelay(sock)
```

`_set_nodelay` se llama sin condicion en el `__init__` de la transport de socket, para
**toda** conexion TCP que asyncio crea -- tanto las que el propio proceso abre
(`create_connection`) como las que acepta un servidor (`create_server`, que es
exactamente el camino de `websockets.serve()`). Se aplica en las dos implementaciones
de event loop de CPython: `_SelectorSocketTransport` (Linux/macOS/Windows-selector) y
`_ProactorSocketTransport` (Windows, el loop por defecto ahi). No es la libreria
`websockets` la que lo activa -- es asyncio, por debajo, siempre.

**Consecuencia:** ninguna conexion de `call_serve.py` necesita activarlo a mano; ya
esta activo desde que la transport se crea, antes de que el primer byte se envie.
`enable_tcp_nodelay()` en `CallSession.__init__` (D44/G0) no estaba mal -- es
defensivo, y protege contra un event loop de terceros (p.ej. `uvloop`) que no
garantice lo mismo -- pero era redundante contra el asyncio stock de CPython. Se deja
tal cual (explicito no estorba); no se anade una segunda llamada en `call_serve.py`
porque no hay nada que activar ahi que no este activo ya.

### D51. Bug critico corregido: `_wait_for_peer` desconectaba al primer participante a los 60 s, en plena llamada

`await asyncio.sleep(PAIR_TIMEOUT_S)` en `_wait_for_peer` no se cancelaba cuando el
segundo participante llegaba. Secuencia real del fallo: A conecta y su handler entra
en el sleep de 60 s; B conecta 5 s despues y `_start_session` arranca la llamada
**en el handler de B**; el handler de A sigue durmiendo los 55 s restantes sin saber
que ya hay sesion; al despertar ve que `_waiting` ya no es suyo, `handle()` retorna, y
`websockets.serve()` cierra la conexion de A automaticamente porque su handler
termino. Resultado: A se cae a los 60 s exactos de conectar, sin importar que la
llamada siguiera activa. En produccion se habria visto como un problema de red o de
Azure sin serlo.

Corregido reestructurando el emparejamiento con un `asyncio.Event` (`paired`) y un
`asyncio.Future` (`finished`) por participante en espera:

- El que llega primero espera `paired.wait()` con `asyncio.wait_for(..., PAIR_TIMEOUT_S)`
  en vez de un `sleep` ciego: si el segundo llega antes, `paired.set()` interrumpe la
  espera de inmediato, no a los 60 s.
- Tras emparejar, el handler del primero se queda vivo en `await finished` mientras
  dura la sesion entera, en vez de retornar y dejar que la libreria cierre su socket.
- `_start_session` marca `finished` en un `finally`, así que si `CallSession(...)` o
  `session.run()` lanzan excepcion, el primero se libera igual -- no se queda colgado
  esperando un `finished` que nunca llegaria.

Verificado con una prueba nueva (`tests/test_lobby_pairing.py`, sin motores reales, sin
tocar Deepgram/Groq/Fish): A conecta, B conecta a los 5 s, la "llamada" (con
`CallSession` reemplazado por un doble que solo duerme) dura 95 s. A sigue con
`ws.state == State.OPEN` a los 70 s (mas de los 60 s del bug original) y hasta el final
de los 95 s. Antes de la correccion esta prueba no existia porque el bug no se habia
detectado; ahora queda como regresion permanente.

### D52. Dos bugs menores corregidos junto con D51, mismo archivo

- **Validacion de `lang` en el sitio equivocado.** `_participant()` validaba `lang`
  dentro de `_start_session`, que corre en el handler de **B**, fuera del
  `try/except` que `handle()` pone alrededor de `_read_hello()`. Si A mandaba un
  `lang` invalido, la excepcion saltaba recien cuando B se conectaba, dejando las DOS
  conexiones sin resolver. Movida la validacion (contra `SUPPORTED_LANGS`, no solo
  `isinstance(str)`) a `_read_hello()`, donde el `try/except` de cada participante ya
  la cubre individualmente.
- **`json.loads` puede devolver algo que no es dict.** `"null"`, `"[1,2]"` o `"42"` son
  JSON validos; `msg.get(...)` sobre eso lanza `AttributeError`, que no estaba en el
  `except` de `handle()`. Anadido `isinstance(msg, dict)` en `_read_hello()`.

Verificado con la segunda prueba de `tests/test_lobby_pairing.py`: A manda
`{"t":"hello","lang":"xx"}`, su conexion se cierra limpiamente (no se cuelga), y B
conecta despues sin heredar nada del intento fallido de A.

### Deudas anotadas, no resueltas en esta correccion

- **Para G4**: el `Lobby` tiene un solo hueco de espera (`_SLOT_KEY` fijo) y empareja
  por orden de llegada, sin verificar que las dos conexiones pertenezcan a la misma
  llamada. Con 5 slots simultaneos, el participante A de la llamada 1 se emparejaria
  con el A de la llamada 2. `_waiting` ya quedo como `dict[str, _Waiting]` en vez de un
  slot unico precisamente para que G4 no tenga que reescribir el `Lobby` entero: solo
  necesita indexar por `session_id` en vez de la constante `_SLOT_KEY`.
- **Para G2**: `_participant()` lee `lang` directamente del `hello`. El contrato
  (seccion 8) pone el idioma en el **token firmado**, no en el `hello`, precisamente
  para que el cliente no pueda cambiarlo a mitad de sesion. Esto **es una desviacion
  real del contrato**, no una simplificacion inocua -- hoy cualquier cliente que hable
  el protocolo puede mandar el `lang` que quiera en su `hello` sin que nada lo
  contraste contra una sesion autorizada por el Dispatcher. Se cierra cuando G2
  implemente el token firmado y `call_serve.py` lo valide en vez de confiar en el
  campo suelto.

## 2026-08-29 - rama `feature/live-test-app`

### D53. Excepcion justificada: se edito `transcriber.py` (M3) para mandar KeepAlive durante silencios

CLAUDE.md es explicito: "si te encuentras editando... `transcriber.py`: PARA". Se
investigo primero si el fix se podia hacer por composicion desde afuera, como
`EchoGate`/`_ResilientTranslator`/`_ObservingCommitter` en v4 -- **no se pudo**. El
motivo, verificado leyendo el codigo, no supuesto: el bucle que manda audio a Deepgram
(`pump()`, dentro de `DeepgramStreamTranscriber.stream()`) es una funcion anidada
**privada**, no un metodo. No hay atributo, ni clase, ni Protocol que envolver o
heredar desde otro archivo -- la unica forma de tocar ese bucle es editando el archivo.

Se le pregunto al usuario antes de tocar el archivo. Confirmo proceder.

**El problema, medido en una sesion real (no simulado):** el gate (M2) no manda nada
al `pump()` mientras no hay habla -- deliberado, es lo que hace que el costo de STT sea
por minuto de habla y no de reloj. Pero un silencio real (la persona piensa, escucha al
otro) deja el socket sin nada que enviar, y Deepgram lo cierra por inactividad:

```
WARNING sincro.transcriber: deepgram ws down (received 1011 (internal error)
Deepgram did not receive audio data or a text message within the timeout window...)
```

**El cambio**, minimo y quirurgico: en `pump()`, el `await arrived.wait()` sin limite
(esperaba indefinidamente al siguiente frame de audio) paso a `asyncio.wait_for(...,
timeout=WS_KEEPALIVE_INTERVAL_S)` (5 s). Si se cumple el timeout sin que llegue audio
nuevo, se manda `conn.send_keep_alive()` (metodo oficial del SDK de Deepgram para
`/v1/listen`, no una construccion manual) y se vuelve a esperar. Si el propio
`send_keep_alive()` fallara, el error sale por el `except Exception` que ya maneja
reconexion con backoff (F6) -- no se traga silenciosamente.

**Verificado, no supuesto:** script aparte que alimenta el transcriber con 1 s de
"habla" (ruido, alcanza para probar la conexion) y despues 17 s de silencio real
(mas largo que las pausas que causaban el corte en una sesion real):

```
reconnects: 0   keepalives_sent: 3   (a los 5s, 10s y 15s del silencio)
```

Antes del fix, ese mismo hueco de silencio disparaba una reconexion. `make check` y el
resto de la suite de regresion (`ws-test`/`call-test` no tocan este camino con
silencios largos, corren limpios igual) siguen pasando.

Se anade `self.keepalives_sent` como contador publico, mismo patron que
`reconnects`/`downtime_s`, para que quede visible en el resumen de sesion y no sea un
mecanismo invisible.

### Rechazado en el camino: "Estrategia A" (VAD nativo de Deepgram, sin VAD local)

Antes de llegar al KeepAlive, se evaluo una propuesta externa mas grande: sacar el VAD
local (Silero) y mandarle todo el audio a Deepgram sin filtrar, confiando en su propio
endpointing. Rechazada con evidencia ya existente en este mismo archivo, no nueva:

- La propuesta asumia que `endpointing=200` baja la espera de habla de ~800 ms a
  200 ms. D38 ya midio lo contrario con n=40: el P90 empeora 616 ms.
- La propuesta asumia que el VAD local anade retraso por el buffer de arranque
  (front-clipping). `PREFIX_FRAMES` (240 ms) ya resuelve eso sin costo: es un buffer
  rotatorio que se vacia de una sola vez al detectar habla, no una espera secuencial.
- La propuesta asumia que sacar el VAD local reduce la latencia hacia Deepgram. D39 ya
  lo midio: 1009 ms con gate contra 1023 ms sin el. Sin diferencia.

Lo unico real de la propuesta -- que una conexion sin trafico se cae por inactividad --
es justo lo que el KeepAlive arregla sin resignar el ahorro de costo de filtrar
silencio antes de mandarlo a Deepgram.
