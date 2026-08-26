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

### D13. Directiva para F2: M3 usa el SDK oficial de Deepgram - NO IMPLEMENTADA

Registrada aqui para que F2 no repita el analisis de D7. **No se implementa en esta
tarea**: el camino WebSocket de M3 es F2.

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
