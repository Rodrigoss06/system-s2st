# Guion de demo por consola — 3 minutos

Demo de SINCRO Engine v3 en terminal. Cuatro bloques, en este orden: la traduccion
funcionando, el contraste de voz clonada contra voz neutra, una frase con numeros y
fechas exactos, y la tabla de metricas.

**Duracion objetivo: 3 minutos.** Los tiempos de abajo son acumulados.

---

## Antes de empezar (no cuenta en los 3 min)

Preparalo con antelacion. Nada de esto debe ocurrir delante del publico.

```bash
# 1. El entorno responde y las tres credenciales valen
make check

# 2. Timbre enrolado y su reference_id ya en .env
make enroll REF=tests/fixtures/voz_referencia.wav SPEAKER=demo
#    -> copia el reference_id a SINCRO_VOICE_ID en .env

# 3. Pesos del turn-detector ya descargados: la primera vez tarda y arruina el ritmo
make live ARGS="--from-wav tests/fixtures/es_30s.wav --seconds 20"

# 4. Telemetria reciente para el bloque 4
make report
```

**Checklist fisico:**

- [ ] Auriculares puestos. Sin ellos el motor se realimenta y entra en bucle.
- [ ] Microfono probado: `make live ARGS=--devices`
- [ ] Dos terminales abiertas, fuente grande, `clear` hecho
- [ ] `.env` con `SINCRO_SRC_LANG=es` y `SINCRO_DST_LANG=en`

---

## Bloque 1 — La traduccion funciona (0:00 → 1:00)

> «Esto es un motor de doblaje. Hablo en espanol y sale en ingles, con mi voz.»

```bash
make live
```

Aparece el aviso de auriculares. **Leelo en voz alta**: es parte del argumento, no un
tramite. Confirma con `s`.

Habla estas tres frases, **pausando entre ellas** para que el endpointing cierre turno:

1. «Buenos dias a todos, gracias por conectarse a la reunion de esta manana.»
2. «Hoy vamos a revisar el informe del tercer trimestre.»
3. «El equipo esta trabajando bien y vamos por buen camino.»

Cada turno imprime el trigger, el TTFA en milisegundos, el texto origen y el destino.

> «Fijaos en el TTFA: es el tiempo desde que dejo de hablar hasta que suena la primera
> silaba doblada.»

`Ctrl-C` para terminar. El resumen de sesion imprime P50/P90/P99, la distribucion de
triggers y el estado de la puerta anti-eco.

**Si algo falla:** sigue hablando. Un turno que sale mal no rompe la demo; el motor no se
cae. Si el TTS cayera, el turno saldria marcado `[SUBTITULO: TTS caido]` con la traduccion
en pantalla, que es exactamente lo que se quiere ensenar.

---

## Bloque 2 — Voz clonada contra voz neutra (1:00 → 2:00)

Es el contraste que justifica el proyecto. **Voz neutra primero**, para que la clonada
llegue despues y se note.

```bash
make live NEUTRAL=1
```

> «Misma frase, pero con una voz cualquiera de catalogo.»

Di: «Buenos dias, me llamo Rodrigo y trabajo desde Arequipa.»

`Ctrl-C`. Ahora la clonada:

```bash
make live
```

> «Ahora con mi timbre, clonado de veinte segundos de grabacion. Misma frase.»

Repite exactamente la misma frase.

> «El texto es identico. Lo unico que cambia es quien parece estar hablando.»

**Refuerzo si hay tiempo:** el clip de referencia esta en espanol y la salida es en
ingles. Fish transporta el timbre entre idiomas; no hace falta una grabacion por idioma.

---

## Bloque 3 — Numeros, fechas y nombres propios (2:00 → 2:30)

El bloque que convence a quien decide. Un doblaje que se inventa una cifra no sirve.

Con la sesion clonada aun abierta, di **despacio y vocalizando**:

> «Las ventas subieron doce por ciento y necesitamos el presupuesto aprobado antes del
> quince de marzo de dos mil veintisiete.»

Deberia salir algo como:

```
Sales rose 12 percent and we need the budget approved before March 15, 2027.
```

> «Doce por ciento sigue siendo doce. El quince de marzo de dos mil veintisiete sigue
> siendo esa fecha. El prompt del traductor lo exige de forma explicita: numeros,
> cantidades, fechas y nombres propios se preservan literalmente.»

**Salvedad honesta, dila tu antes de que la pregunten:** los nombres propios se preservan
**en el texto**, pero la voz inglesa a veces los pronuncia con fonetica inglesa. `Arequipa`
sale escrito bien y sonando raro. Esta registrado como riesgo R9 y no esta resuelto.

`Ctrl-C` para cerrar la sesion.

---

## Bloque 4 — La tabla de metricas (2:30 → 3:00)

> «Nada de esto es una impresion. Cada segmento deja una linea de telemetria.»

```bash
make report
```

Muestra en pantalla:

- **TTFA P50 / P90 / P99.** El objetivo de la fase de streaming es P90 por debajo de 2 s.
- **Distribucion de triggers.** `eou` es cierre semantico, `punctuation` es puntuacion,
  `timeout` y `max_len` son las valvulas de seguridad. Si `timeout` sube del 10 %, el
  endpointing esta mal calibrado.
- **Curva de deriva en ASCII.** Cuanto se adelanta o se atrasa el doblaje respecto al
  hablante.
- **Coste acumulado**, en dolares y por minuto de habla.

> «Un camino de codigo sin telemetria es un camino que no se puede verificar. Por eso la
> telemetria se construyo en la primera fase, antes que ningun modulo real.»

**Cierre:**

> «Ocho modulos, agnosticos del transporte. Hoy entra por microfono; manana entra por una
> pista de LiveKit sin tocar el motor. Y todo lo que no funciona esta escrito en
> `STATE.md` y `DECISIONS.md`, con la evidencia al lado.»

---

## Plan B si se cae la red

No improvises delante del publico. Ten esto preparado:

```bash
# Doblaje offline sobre un WAV fijo. No necesita microfono, si necesita las APIs.
make dub-file IN=tests/fixtures/es_30s.wav

# Si tampoco hay APIs: la cascada de fakes, sin red, siempre funciona.
make check
```

Y un WAV ya doblado de una corrida anterior en `out/`, listo para reproducir.

---

## Lo que NO hay que prometer

Decir esto de antemano gana mas credibilidad que ocultarlo:

- El TTFA en vivo **aun no cumple** el objetivo de 2 s en el percentil 90. El desglose por
  etapas dice que dos tercios del tiempo estan entre que el hablante calla y que se cierra
  el segmento; el LLM y el TTS no son el cuello de botella.
- La deriva no cumple el criterio literal de 300 ms. El doblaje **nunca va por detras**,
  que es la direccion que hace dano, pero termina antes que la fuente y esa holgura se
  acumula.
- El panel de escucha de la clonacion **esta pendiente**: hay medida objetiva de que el
  timbre se transporta, no hay tres personas que lo hayan confirmado.
- Es **un hablante**. Multi-participante es v4.

Todo esto esta en `STATE.md` con su evidencia y en `DECISIONS.md` con su motivo.
