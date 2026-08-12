#!/bin/sh
# Validate the Pi Linux sandbox without starting the backend or changing PiHarness.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
launcher="$script_dir/pi-sandbox"

if [ "$(uname -s)" != "Linux" ]; then
  printf '%s\n' "[error] El sandbox estricto v1 requiere Linux." >&2
  printf '%s\n' "En macOS desarrolla el backend, pero habilita PI_ENABLED solo en el host Linux." >&2
  exit 1
fi

missing=
for command_name in bwrap socat prlimit node; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    missing="$missing $command_name"
  fi
done
if [ -n "$missing" ]; then
  printf '%s\n' "[error] Faltan dependencias:$missing" >&2
  printf '%s\n' "Debian/Ubuntu: sudo apt-get install bubblewrap socat util-linux" >&2
  printf '%s\n' "Fedora/RHEL:   sudo dnf install bubblewrap socat util-linux" >&2
  printf '%s\n' "Arch:          sudo pacman -S bubblewrap socat util-linux" >&2
  exit 1
fi

if [ ! -x "$repo_dir/node_modules/.bin/pi" ]; then
  printf '%s\n' "[error] Pi no esta instalado. Ejecuta 'pnpm install' en $repo_dir." >&2
  exit 1
fi
if [ ! -x "$launcher" ]; then
  chmod 0755 "$launcher"
fi

"$launcher" --check
printf '%s\n' "[ok] El launcher falla cerrado y esta listo para PI_BIN=$launcher"
