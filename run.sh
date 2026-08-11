#!/bin/sh
# Arranca el backend del wrapper. Uso: ./run.sh [--port N]
# Requiere el venv (crealo con: python3.12 -m venv .venv && .venv/bin/pip install cryptography)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "[error] no hay .venv. Crea el entorno:" >&2
  echo "  python3.12 -m venv .venv && .venv/bin/pip install cryptography" >&2
  exit 1
fi
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
exec .venv/bin/python -m go_backend.server serve "$@"
