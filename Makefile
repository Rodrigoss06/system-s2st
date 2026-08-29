# SINCRO Engine v3. Un target por fase; los no implementados fallan con mensaje claro.
.DEFAULT_GOAL := help
SHELL := /bin/bash

PY_VERSION := 3.13
VENV := .venv
PY := $(VENV)/bin/python

# El motor no se ejecuta desde el interprete del sistema: el documento tecnico exige
# Python 3.11 a 3.13 y Arch trae 3.14.
define REQUIRE_VENV
	@test -x "$(PY)" || { \
		echo "ERROR: falta el entorno virtual. Ejecuta primero: make setup"; exit 1; }
endef

# Un target de fase futura no debe fallar con un ImportError confuso. Falla aqui.
define NOT_IMPLEMENTED
	@echo "ERROR: '$(1)' pertenece a $(2) y todavia no esta implementado."; \
	 echo "       Fase activa segun STATE.md: $$(grep -m1 '^Fase activa:' STATE.md | cut -d: -f2- | xargs)"; \
	 echo "       Cierra $(2) con evidencia en STATE.md antes de usar este target."; \
	 exit 1
endef

.PHONY: help setup check dub-file live enroll enroll-delete drift-test matrix-test soak report lint clean

help:
	@echo "SINCRO Engine v3 - targets"
	@echo ""
	@echo "  make setup                 Crea .venv con Python $(PY_VERSION) e instala dependencias"
	@echo "  make check                 F0  Valida las 3 credenciales y emite un JSONL con fakes"
	@echo "  make dub-file IN=x.wav     F1  Cascada offline sobre archivo (OUT=... opcional)"
	@echo "  make live                  F2  Microfono a altavoz (MIN_SILENCE=, SECONDS=, NEUTRAL=1)"
	@echo "  make enroll REF=voz.wav    F3  Registra timbre, devuelve reference_id"
	@echo "  make enroll-delete ID=x        Borra una huella vocal de Fish"
	@echo "  make drift-test            F4  WAV de 10 min, mide deriva acumulada (DRIFT_WAV=)"
	@echo "  make matrix-test           F5  Los 20 pares dirigidos"
	@echo "  make soak MIN=20           F6  Sesion larga con corte de red (CUT_AT=, WAV=)"
	@echo "  make report                    Agrega el JSONL: P50 P90 P99, triggers, deriva, coste"
	@echo ""
	@echo "  make lint                  ruff + mypy strict"
	@echo "  make clean                 Borra out/ y caches"

setup:
	@command -v uv >/dev/null || { echo "ERROR: uv no esta instalado. https://docs.astral.sh/uv/"; exit 1; }
	uv venv --python $(PY_VERSION) $(VENV)
	uv pip install --python $(PY) -e ".[dev]"
	$(PY) -m livekit.agents download-files
	@echo ""
	@echo "Listo. Copia .env.example a .env y rellena las 3 credenciales."

check:
	$(REQUIRE_VENV)
	@test -f .env || { echo "ERROR: falta .env. Copia .env.example y rellena las 3 credenciales."; exit 1; }
	$(PY) -m sincro.check

dub-file:
	$(REQUIRE_VENV)
	@test -n "$(IN)" || { echo "ERROR: falta IN. Uso: make dub-file IN=tests/fixtures/es_30s.wav"; exit 1; }
	@test -f "$(IN)" || { echo "ERROR: no existe $(IN)"; exit 1; }
	$(PY) -m sincro.dub_file --in "$(IN)" $(if $(OUT),--out "$(OUT)",)

live:
	$(REQUIRE_VENV)
	$(PY) -m sincro.live $(if $(MIN_SILENCE),--min-silence $(MIN_SILENCE),) $(if $(SECONDS),--seconds $(SECONDS),) $(if $(NEUTRAL),--neutral-voice,) $(ARGS)

enroll:
	$(REQUIRE_VENV)
	@test -n "$(REF)" || { echo "ERROR: falta REF. Uso: make enroll REF=voz.wav"; exit 1; }
	@test -f "$(REF)" || { echo "ERROR: no existe $(REF)"; exit 1; }
	$(PY) -m sincro.enroll --ref "$(REF)" $(if $(SPEAKER),--speaker "$(SPEAKER)",) $(ARGS)

enroll-delete:
	$(REQUIRE_VENV)
	@test -n "$(ID)" || { echo "ERROR: falta ID. Uso: make enroll-delete ID=<reference_id>"; exit 1; }
	$(PY) -m sincro.enroll --delete "$(ID)"

DRIFT_WAV ?= tests/fixtures/es_10min.wav

drift-test:
	$(REQUIRE_VENV)
	@test -f "$(DRIFT_WAV)" || { \
		echo "ERROR: falta $(DRIFT_WAV)."; \
		echo "       Generalo con: $(PY) tests/make_longform.py --minutes 10 --out $(DRIFT_WAV)"; \
		exit 1; }
	$(PY) -m sincro.dub_file --in "$(DRIFT_WAV)" --curve
	@echo ""
	$(PY) -m sincro.report

matrix-test:
	$(REQUIRE_VENV)
	$(PY) -m sincro.matrix $(if $(ONLY),--only $(ONLY),) $(ARGS)

soak:
	$(REQUIRE_VENV)
	$(PY) -m sincro.soak --minutes $(if $(MIN),$(MIN),20) $(if $(WAV),--wav "$(WAV)",) $(if $(CUT_AT),--cut-at $(CUT_AT),) $(ARGS)

report:
	$(REQUIRE_VENV)
	$(PY) -m sincro.report $(if $(FILE),--file "$(FILE)",)

lint:
	$(REQUIRE_VENV)
	$(VENV)/bin/ruff check src/
	$(VENV)/bin/mypy

clean:
	rm -rf out/*.jsonl out/*.wav .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
