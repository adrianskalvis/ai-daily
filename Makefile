# One-command local run: builds data/ then serves the site on http://localhost:8000
PY ?= python3
PORT ?= 8000

.PHONY: run build serve test install

install:
	$(PY) -m pip install -q -r requirements.txt

build: install
	$(PY) scripts/build.py

serve:
	@echo "Open http://localhost:$(PORT)  (Ctrl+C to stop)"
	$(PY) -m http.server $(PORT) --bind 127.0.0.1

run: build serve

test: install
	$(PY) -m pytest -q
