"""live_app.py - pequena aplicacion de escritorio para probar el motor v3 en vivo.

Ventana Tkinter (stdlib, sin dependencias nuevas) sobre la MISMA construccion de motor
que `live.py` (`run_live`): microfono real, `DubbingEngine` real, parlante real. No es
un motor nuevo ni toca el nucleo -- es una interfaz mas comoda que la consola para
`make live`, pensada para ver el texto fuente y la traduccion en vivo sin perder el
scroll de la terminal.

El motor corre en un hilo aparte con su propio loop de asyncio; Tkinter vive en el hilo
principal (su mainloop es sincrono, no puede compartir loop con asyncio). Los dos se
comunican por una `queue.Queue` -- el hilo del motor solo escribe, la GUI solo lee, sin
locks.

Auriculares obligatorios, igual que `make live`: el aviso aparece ANTES de abrir el
microfono, con un dialogo que hay que confirmar (CLAUDE.md, "nunca desactivar la
segunda defensa porque tengo auriculares").

Uso: .venv/bin/python -m sincro.live_app
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters.console_io import MicrophoneSource, SpeakerSink, check_devices
from .committer import StreamingCommitter
from .config import Settings, load_settings
from .contracts import DubbedChunk
from .engine import DubbingEngine, TurnResult
from .gate import DEFAULT_MIN_SILENCE, SileroGate
from .report import percentile
from .synthesizer import FishSynthesizer
from .telemetry import TelemetryWriter
from .transcriber import DeepgramStreamTranscriber
from .translator import GroqTranslator
from .voices import FishVoiceRegistry


def _fix_tcl_tk_library() -> None:
    """Windows + Miniconda en el PATH: `tcl86t.dll`/`tk86t.dll` existen por duplicado
    (uno correcto en la instalacion de Python, otro en `miniconda3\\Library\\bin`), y
    Windows puede cargar el de Miniconda para este venv -- ese no encuentra su propio
    `init.tcl` porque busca rutas que no coinciden con ninguna instalacion real
    (`TclError: Can't find a usable init.tcl`).

    Fija `TCL_LIBRARY`/`TK_LIBRARY` a la instalacion REAL de este interprete
    (`sys.base_prefix`, no `sys.prefix`: en un venv apunta a la instalacion base, no a
    `.venv`) antes de importar `tkinter`, que es cuando se resuelve la DLL. Solo actua
    si faltan y si la carpeta existe -- en Linux/macOS, o si ya estan bien puestas, no
    hace nada.
    """
    if os.name != "nt":
        return
    base = Path(sys.base_prefix) / "tcl"
    tcl_dir = next(base.glob("tcl8.*"), None)
    tk_dir = next(base.glob("tk8.*"), None)
    if tcl_dir is not None and not os.environ.get("TCL_LIBRARY"):
        os.environ["TCL_LIBRARY"] = str(tcl_dir)
    if tk_dir is not None and not os.environ.get("TK_LIBRARY"):
        os.environ["TK_LIBRARY"] = str(tk_dir)


_fix_tcl_tk_library()

import tkinter as tk  # noqa: E402 (tiene que ir despues de _fix_tcl_tk_library)
from tkinter import messagebox, scrolledtext  # noqa: E402

TTS_SAMPLE_RATE = 44_100


@dataclass
class _UiEvent:
    kind: str  # "status" | "turn" | "summary" | "error"
    payload: dict[str, Any] = field(default_factory=dict)


class EngineThread(threading.Thread):
    """Un hilo, un loop de asyncio, un DubbingEngine. Construccion identica a
    `live.run_live`: solo cambia a donde van los resultados (una Queue, no print)."""

    def __init__(
        self,
        settings: Settings,
        neutral_voice: bool,
        events: queue.Queue[_UiEvent],
    ) -> None:
        super().__init__(daemon=True)
        self._s = settings
        self._neutral_voice = neutral_voice
        self._events = events
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    def stop(self) -> None:
        """Se llama desde el hilo de Tkinter. `call_soon_threadsafe` es obligatorio:
        el Event vive en el loop del otro hilo."""
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            self._events.put(_UiEvent("error", {"message": f"{type(e).__name__}: {e}"}))
            self._events.put(_UiEvent("status", {"text": "Error"}))
        finally:
            self._loop.close()

    async def _main(self) -> None:
        s = self._s
        profile = s.profile

        self._events.put(_UiEvent("status", {"text": "Cargando turn-detector..."}))
        gate = SileroGate(profile, min_silence_duration=DEFAULT_MIN_SILENCE)
        gate.load_eou()

        transcriber = DeepgramStreamTranscriber(
            s.deepgram_api_key, profile.src, profile.deepgram_code
        )
        committer = StreamingCommitter(gate, profile.turn_detector_code)
        translator = GroqTranslator(
            s.groq_api_key,
            s.llm_model,
            profile,
            reasoning_effort=s.llm_reasoning_effort,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
        )
        synth = FishSynthesizer(s.fish_api_key, model=s.tts_model, sample_rate=TTS_SAMPLE_RATE)
        writer = TelemetryWriter()

        registry = FishVoiceRegistry(s.fish_api_key)
        reference_id = ""
        if not self._neutral_voice and s.voice_id:
            registry.remember("speaker-0", s.voice_id)
            reference_id = s.voice_id

        speaker = SpeakerSink(TTS_SAMPLE_RATE)
        speaker.open()

        async def on_audio(chunk: DubbedChunk) -> None:
            await speaker.play(chunk.pcm)

        engine = DubbingEngine(
            s, gate, transcriber, committer, translator, synth, writer,
            on_audio=on_audio, reference_id=reference_id,
        )

        def on_turn(r: TurnResult) -> None:
            self._events.put(_UiEvent("turn", {
                "trigger": r.seg.trigger,
                "ttfa_ms": r.ttfa_ms,
                "src": r.seg.text,
                "dst": r.text_dst or "(vacio)",
                "dropped": r.dropped,
                "tts_failed": r.tts_failed,
            }))

        mic = MicrophoneSource()
        self._stop_event = asyncio.Event()
        self._events.put(_UiEvent("status", {"text": "Escuchando..."}))

        runner = asyncio.create_task(engine.run(mic.frames(), on_turn=on_turn))
        waiter = asyncio.create_task(self._stop_event.wait())
        _done, pending = await asyncio.wait(
            {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        await engine.aclose()
        speaker.close()

        st = engine.stats
        summary: dict[str, Any] = {"turns": st.turns, "triggers": dict(st.triggers)}
        if st.ttfa_ms:
            v = [float(x) for x in st.ttfa_ms]
            summary["p50"] = percentile(v, 0.50)
            summary["p90"] = percentile(v, 0.90)
            summary["p99"] = percentile(v, 0.99)
        self._events.put(_UiEvent("summary", summary))
        self._events.put(_UiEvent("status", {"text": "Detenido"}))


class LiveApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("SINCRO - traduccion en vivo")
        root.geometry("760x560")

        self.status_var = tk.StringVar(value="Detenido")
        tk.Label(root, textvariable=self.status_var, font=("Segoe UI", 12, "bold")).pack(pady=6)

        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=4)
        self.start_btn = tk.Button(btn_frame, text="Iniciar", width=14, command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = tk.Button(
            btn_frame, text="Detener", width=14, command=self.on_stop, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=4)

        self.neutral_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            root, text="Voz neutra (en vez de la clonada)", variable=self.neutral_var
        ).pack()

        self.log = scrolledtext.ScrolledText(root, wrap="word", height=26)
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.log.configure(state="disabled")

        self._events: queue.Queue[_UiEvent] = queue.Queue()
        self._thread: EngineThread | None = None
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_start(self) -> None:
        confirmed = messagebox.askyesno(
            "Auriculares obligatorios",
            "El microfono y el altavoz estan en la misma maquina.\n\n"
            "Sin auriculares el motor transcribe su propia salida traducida y entra "
            "en bucle de realimentacion.\n\n¿Llevas auriculares puestos?",
        )
        if not confirmed:
            return

        ok_dev, info = check_devices()
        if not ok_dev:
            messagebox.showerror(
                "Error", f"No se pudieron listar los dispositivos de audio:\n{info}"
            )
            return
        self._append(f"[dispositivos] {info}")

        try:
            settings = load_settings()
        except Exception as e:
            messagebox.showerror("Error de configuracion", f"{type(e).__name__}: {e}")
            return

        self._append(
            f"[modelo] {settings.llm_model}  reasoning_effort={settings.llm_reasoning_effort}"
        )
        self._thread = EngineThread(settings, self.neutral_var.get(), self._events)
        self._thread.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def on_stop(self) -> None:
        if self._thread is not None:
            self._thread.stop()
        self.stop_btn.configure(state="disabled")

    def on_close(self) -> None:
        self.on_stop()
        self.root.after(300, self.root.destroy)

    def _poll(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, event: _UiEvent) -> None:
        if event.kind == "status":
            self.status_var.set(event.payload["text"])
        elif event.kind == "turn":
            p = event.payload
            if p["dropped"]:
                tag = " [DROP]"
            elif p["tts_failed"]:
                tag = " [SUBTITULO: TTS caido]"
            else:
                tag = ""
            self._append(f"\n[{p['trigger']}] TTFA {p['ttfa_ms']} ms{tag}")
            self._append(f"  es: {p['src']}")
            self._append(f"  en: {p['dst']}")
        elif event.kind == "summary":
            p = event.payload
            self._append("\n--- resumen de la sesion ---")
            self._append(f"turnos: {p.get('turns', 0)}")
            if "p50" in p:
                self._append(
                    f"TTFA  P50 {p['p50']:.0f} ms   P90 {p['p90']:.0f} ms   P99 {p['p99']:.0f} ms"
                )
            self._append(f"triggers: {p.get('triggers', {})}")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        elif event.kind == "error":
            messagebox.showerror("Error", event.payload["message"])
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    LiveApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
