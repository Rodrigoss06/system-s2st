# SINCRO Engine v3

Motor de doblaje voz a voz en tiempo real con clonacion de timbre. Toma audio de un
hablante, lo traduce y lo reproduce con **su propia voz** en el idioma destino.

Alcance de v3: nucleo aislado, demo por consola, un hablante, idioma declarado.
Sin sala, sin navegador, sin backend.

```
microfono -> VAD -> STT -> segmentacion -> traduccion -> TTS clonado -> altavoz
```

| Capa | Componente | Coste |
|---|---|---|
| VAD | Silero (`livekit-plugins-silero`) | local, $0 |
| Endpointing | LiveKit turn-detector multilingue | local, $0 |
| STT | Deepgram Nova-3 Monolingual | $0.0077/min |
| Traduccion | Groq · `qwen/qwen3.6-27b` | $0.60 / $3.00 por M tokens |
| TTS | Fish Audio `s2.1-pro` (dev: `s2.1-pro-free`) | $15 / M bytes UTF-8 |
| Audio I/O | `sounddevice` (PortAudio) | $0 |

Idiomas: **es, en, pt-BR, fr, ja**. Declarados por variable de entorno, sin autodeteccion.

> **Estado del proyecto:** lee [`STATE.md`](STATE.md). Varias fases estan cerradas con
> salvedades explicitas y algun criterio sin verificar. [`DECISIONS.md`](DECISIONS.md)
> registra los 30 desvios del plan con su motivo.

---

## 1. Requisitos previos

### Todas las plataformas

- **Python 3.11, 3.12 o 3.13.** No 3.14: varias dependencias aun no publican ruedas.
  No hace falta instalarlo a mano, `uv` lo descarga (ver abajo).
- **[uv](https://docs.astral.sh/uv/)**, el gestor de entornos.
- **Tres claves de API** (las tres tienen plan gratuito suficiente para desarrollo):
  - Deepgram — https://console.deepgram.com → API Keys
  - Groq — https://console.groq.com/keys
  - Fish Audio — https://fish.audio/go-api/ → API Keys
- **Auriculares.** No son opcionales: ver [Anti-eco](#6-anti-eco-lee-esto-antes-de-make-live).

### Instalar `uv`

<details open>
<summary><b>Linux / macOS</b></summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
</details>

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
</details>

### Dependencias del sistema para el audio

`sounddevice` necesita PortAudio y `soundfile` necesita libsndfile.

<details open>
<summary><b>Linux</b></summary>

```bash
# Arch / Manjaro
sudo pacman -S portaudio libsndfile

# Debian / Ubuntu / Mint
sudo apt update && sudo apt install -y portaudio19-dev libsndfile1 ffmpeg

# Fedora
sudo dnf install -y portaudio-devel libsndfile ffmpeg
```
</details>

<details>
<summary><b>macOS</b></summary>

```bash
brew install portaudio libsndfile ffmpeg
```

En Apple Silicon, si `sounddevice` no encuentra PortAudio:

```bash
export DYLD_LIBRARY_PATH="$(brew --prefix portaudio)/lib:$DYLD_LIBRARY_PATH"
```

La primera vez que ejecutes `make live`, macOS pedira permiso de microfono.
Concedelo en *Ajustes del Sistema → Privacidad y seguridad → Microfono*.
</details>

<details>
<summary><b>Windows</b></summary>

**Nada que instalar.** Las ruedas de `sounddevice` y `soundfile` para Windows ya traen
PortAudio y libsndfile incrustados.

`ffmpeg` solo hace falta si vas a convertir WAV a mano:
```powershell
winget install Gyan.FFmpeg
```
</details>

---

## 2. Instalacion

```bash
git clone https://github.com/Rodrigoss06/system-s2st.git
cd system-s2st
```

<details open>
<summary><b>Linux / macOS</b></summary>

```bash
make setup
```
</details>

<details>
<summary><b>Windows</b></summary>

Windows no trae `make`. Tienes dos caminos:

**Opcion A — instalar make** (recomendado, deja funcionar todos los comandos del proyecto):
```powershell
winget install GnuWin32.Make
# o, si usas Chocolatey:
choco install make
```
Luego usa `make setup`, `make check`, etc. igual que en Linux.

**Opcion B — comandos directos**, sin `make`:
```powershell
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
```
La [tabla de equivalencias](#5-comandos) de la seccion 5 traduce cada target.
</details>

Esto crea `.venv` con Python 3.13 e instala todo. Tarda unos minutos: `livekit-agents`
y sus plugins son pesados.

### Configurar las credenciales

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

Abre `.env` y rellena las tres claves. **`.env` esta en `.gitignore`: nunca se sube.**

### Verificar que todo funciona

```bash
make check
```

Valida las tres credenciales con una llamada minima a cada API y emite un JSONL de prueba
usando implementaciones falsas que no tocan la red. Debe terminar asi:

```
[1/2] credentials
  PASS  deepgram  HTTP 200
  PASS  groq      HTTP 200
  PASS  fish      HTTP 200

[2/2] fake pipeline telemetry
  PASS  6 segments, 6/6 stage marks each

RESULT: PASS - 3/3 credentials valid, telemetry JSONL emitted.
```

Si sale `FAIL`, el mensaje distingue entre credencial ausente, credencial rechazada y
proveedor inalcanzable.

---

## 3. Primer uso

### Generar los audios de prueba

Los WAV no se versionan (pesan y se regeneran). Creense con Fish TTS, coste cero con
`s2.1-pro-free`:

```bash
# Linux / macOS
.venv/bin/python tests/make_fixture.py --kind fixture --lang es --out tests/fixtures/es_30s.wav

# Windows
.venv\Scripts\python.exe tests\make_fixture.py --kind fixture --lang es --out tests\fixtures\es_30s.wav
```

Otros fixtures, segun lo que quieras ejecutar:

| Para | Comando |
|---|---|
| `make dub-file` | `--kind fixture --lang es --out tests/fixtures/es_30s.wav` |
| `make enroll` | `--kind reference --lang es --voice-id dfa5b230c8054f429e434f4a6e9bbdec --out tests/fixtures/voz_referencia.wav` |
| `make matrix-test` | `--kind matrix --lang <es\|en\|pt-BR\|fr\|ja> --out tests/fixtures/matrix_<lang>.wav` (los cinco) |
| `make drift-test` | `tests/make_longform.py --minutes 10 --out tests/fixtures/es_10min.wav` |

### Doblar un archivo (lo mas facil para empezar)

```bash
make dub-file IN=tests/fixtures/es_30s.wav
```

Sin microfono, sin VAD, sin streaming: WAV entra, WAV doblado sale en `out/`. Cada corrida
sobre el mismo WAV es identica, asi que si algo cambia sabes que fue el modelo y no el
driver de audio.

### Ver las metricas

```bash
make report
```

Agrega el JSONL de telemetria: P50/P90/P99 de TTFA, distribucion de triggers, curva de
deriva y coste acumulado.

---

## 4. Sesion en vivo

```bash
make live
```

Antes de abrir el microfono imprime el aviso de auriculares y **espera confirmacion**.
Habla; el doblaje sale por el altavoz. `Ctrl-C` para terminar y ver el resumen.

Opciones utiles:

```bash
make live NEUTRAL=1                      # voz neutra, para contrastar con la clonada
make live MIN_SILENCE=0.30               # cuanto silencio marca fin de frase (min 0.25)
make live SECONDS=60                     # corta sola a los 60 s
make live ARGS="--from-wav tests/fixtures/es_30s.wav"   # sin microfono, para probar
make live ARGS=--devices                 # lista los dispositivos de audio
```

> La primera ejecucion descarga los pesos del turn-detector (unos cientos de MB) desde
> HuggingFace. Solo la primera vez.

### Clonar tu voz

```bash
make enroll REF=tests/fixtures/voz_referencia.wav SPEAKER=rodrigo
```

Pide **consentimiento explicito** antes de subir nada, porque la huella vocal es dato
biometrico: con ella se puede sintetizar habla que suena como el hablante diciendo cosas
que nunca dijo. Devuelve un `reference_id`; ponlo en `SINCRO_VOICE_ID` dentro de `.env`.

Para grabar tu voz real en vez de usar el clip sintetico: 15-20 s hablando **en tu idioma
nativo**, mono 16 kHz. Fish transfiere el timbre al idioma destino.

```bash
ffmpeg -i tu_grabacion.m4a -ac 1 -ar 16000 tests/fixtures/voz_referencia.wav
```

Cuando termines, borra la huella del servidor:

```bash
make enroll-delete ID=<reference_id>
```

---

## 5. Comandos

| `make` (Linux/macOS/Windows con make) | Equivalente directo | Que hace |
|---|---|---|
| `make setup` | ver seccion 2 | Crea `.venv` e instala dependencias |
| `make check` | `python -m sincro.check` | Valida las 3 credenciales + JSONL de prueba |
| `make dub-file IN=x.wav` | `python -m sincro.dub_file --in x.wav` | Cascada offline sobre archivo |
| `make live` | `python -m sincro.live` | Microfono a altavoz |
| `make enroll REF=v.wav` | `python -m sincro.enroll --ref v.wav` | Registra timbre |
| `make enroll-delete ID=x` | `python -m sincro.enroll --delete x` | Borra la huella de Fish |
| `make drift-test` | `python -m sincro.dub_file --in tests/fixtures/es_10min.wav --curve` | Deriva sobre 10 min |
| `make matrix-test` | `python -m sincro.matrix` | Los 20 pares dirigidos |
| `make report` | `python -m sincro.report` | Agrega el JSONL |
| `make lint` | `ruff check src/` y `mypy` | Estilo y tipos |
| `make clean` | — | Borra `out/` y caches |

En Windows sustituye `python` por `.venv\Scripts\python.exe`.
En Linux/macOS por `.venv/bin/python`.

---

## 6. Anti-eco: lee esto antes de `make live`

Microfono y altavoz en la misma maquina se realimentan: el motor **transcribe su propia
salida traducida**, la vuelve a traducir y entra en bucle infinito.

Hay dos defensas y las dos son obligatorias:

1. **Auriculares.** `make live` avisa y pide confirmacion antes de abrir el microfono.
2. **Puerta dura.** `AudioGate` cierra el microfono mientras el sintetizador reproduce y
   lo reabre 150 ms despues de terminar.

La segunda esta siempre activa y **no debe desactivarse "porque tengo auriculares"**.

---

## 7. Problemas frecuentes

<details>
<summary><b><code>make: command not found</code> (Windows)</b></summary>

Windows no trae `make`. Instalalo (`winget install GnuWin32.Make`) o usa los comandos
directos de la tabla de la seccion 5.
</details>

<details>
<summary><b><code>PortAudioError: Error querying device -1</code></b></summary>

No hay dispositivo de audio por defecto. Listalos con `make live ARGS=--devices` y
comprueba que el sistema tenga microfono y salida configurados.
En Linux headless o WSL no hay dispositivos: usa `make live ARGS="--from-wav ..."`.
</details>

<details>
<summary><b><code>OSError: PortAudio library not found</code> (Linux/macOS)</b></summary>

Falta el paquete del sistema. Vuelve a la seccion 1 e instala `portaudio`.
</details>

<details>
<summary><b><code>Error 429 - Rate limit reached ... tokens per day (TPD)</code></b></summary>

El plan gratuito de Groq da **200.000 tokens al dia**. `make matrix-test` consume unos
55.000 y `make drift-test` unos 26.000, asi que se agota rapido.
Espera al reinicio diario, usa `make matrix-test ONLY=es` para correr una sola fuente,
o sube de plan. Ver D30 en `DECISIONS.md`.
</details>

<details>
<summary><b><code>404 model_not_found</code> al traducir</b></summary>

Groq retiro `llama-3.3-70b-versatile` en agosto de 2026. El proyecto usa
`qwen/qwen3.6-27b`. Comprueba que `SINCRO_LLM_MODEL` en `.env` apunta a un modelo que tu
cuenta sirva: `curl -H "Authorization: Bearer $GROQ_API_KEY" https://api.groq.com/openai/v1/models`.
Ver D8.
</details>

<details>
<summary><b>El microfono no se reabre nunca</b></summary>

La puerta anti-eco se quedo cerrada. Es un fallo; reinicia con `Ctrl-C`. El resumen de
sesion imprime `mute_calls` y `unmute_calls`: si no coinciden, reportalo.
</details>

<details>
<summary><b>Python 3.14 instalado y falla la instalacion</b></summary>

Es esperado. `make setup` usa `uv venv --python 3.13`, que descarga un interprete propio
sin tocar el del sistema. Ejecuta siempre a traves de `.venv`.
</details>

---

## 8. Estructura

```
src/sincro/
  contracts.py     tipos y Protocols. Ningun modulo importa transporte de audio
  config.py        M1  perfiles de idioma, factores de expansion
  gate.py          M2  VAD Silero + endpointing + puerta anti-eco
  transcriber.py   M3  Deepgram: REST pre-grabado (F1) y WebSocket (F2)
  committer.py     M4  4 triggers de corte + guardas de no-corte por idioma
  translator.py    M5  Groq + contexto rodante + presupuesto de longitud
  voices.py        M6  enrolamiento de timbre, cache solo en memoria
  synthesizer.py   M7  Fish TTS, streaming, speed acotado 0.95-1.25
  drift.py         M8  controlador de deriva
  telemetry.py     M8  JSONL por segmento
  engine.py        orquestador asincrono
  fakes.py         una implementacion falsa determinista por Protocol
  adapters/        unico lugar que puede tocar sounddevice o livekit.rtc
tests/             generadores de fixtures de audio
out/               telemetria, WAV doblados, factores calibrados
```

**Regla de arquitectura:** el motor es agnostico del transporte. Ningun archivo bajo
`src/sincro/` salvo `adapters/` importa `sounddevice` ni `livekit.rtc`. Cambiar consola
por LiveKit en v4 no obliga a tocar M1-M8.

Cada Protocol tiene un `Fake` determinista en `fakes.py`, que es lo que permite probar
cualquier fase en aislamiento sin gastar credito ni depender de que las APIs esten vivas.

---

## 9. Coste

Por minuto de **habla efectiva**, un hablante, un sentido:

| Capa | $/min |
|---|---|
| STT (Deepgram, con gating VAD) | 0.0077 |
| Traduccion (Groq) | ~0.0021 |
| TTS (Fish `s2.1-pro`) | 0.0159 |
| VAD, endpointing, transporte | 0.0000 |
| **Total** | **~$0.026/min** |

Con `s2.1-pro-free` en desarrollo el TTS es $0 y baja a **~$0.010/min**. El limite que
muerde en la practica no es el dinero sino la **cuota diaria de tokens de Groq**.
