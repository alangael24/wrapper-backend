#!/bin/sh
# Arranca el backend del wrapper. Uso: ./run.sh [--port N]
# Requiere el venv. Si PI_ENABLED=1, instala tambien las dependencias de package.json.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "[error] no hay .venv. Crea el entorno:" >&2
  echo "  python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
if [ "${PI_ENABLED:-0}" = "1" ]; then
  # Default seguro: PiHarness sigue invocando un PI_BIN normal, pero el launcher
  # crea la frontera Bubblewrap antes de ejecutar el binario real de Pi.
  pi_runtime="${PI_BIN:-./scripts/pi-sandbox}"
  PI_BIN=$pi_runtime
  export PI_BIN
  case "$pi_runtime" in
    */*) [ -x "$pi_runtime" ] || {
      echo "[error] Pi no esta instalado o el launcher no es ejecutable: $pi_runtime" >&2
      exit 1
    } ;;
    *) command -v "$pi_runtime" >/dev/null 2>&1 || {
      echo "[error] no se encontro PI_BIN=$pi_runtime" >&2
      exit 1
    } ;;
  esac
  if [ -n "${PI_NODE_BIN_DIR:-}" ]; then
    [ -x "$PI_NODE_BIN_DIR/node" ] || {
      echo "[error] no existe $PI_NODE_BIN_DIR/node" >&2
      exit 1
    }
  elif ! command -v node >/dev/null 2>&1; then
    echo "[error] Pi necesita node en PATH o PI_NODE_BIN_DIR" >&2
    exit 1
  fi
  case "$pi_runtime" in
    ./scripts/pi-sandbox|*/scripts/pi-sandbox)
      "$pi_runtime" --check >/dev/null || {
        echo "[error] el sandbox de Pi no supero el preflight" >&2
        exit 1
      }
      ;;
  esac
fi
exec .venv/bin/python -m go_backend.server serve "$@"
