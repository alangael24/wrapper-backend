#!/bin/sh
# Verifica pi-chrome y muestra la carpeta que debe cargarse como extension unpacked.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(dirname -- "$script_dir")
package_dir="$repo_dir/node_modules/pi-chrome"
pi_extension="$package_dir/extensions/chrome-profile-bridge/index.ts"
browser_extension="$package_dir/extensions/chrome-profile-bridge/browser-extension"

if [ ! -f "$pi_extension" ] || [ ! -f "$browser_extension/manifest.json" ]; then
  printf '%s\n' "[error] pi-chrome no esta instalado en node_modules." >&2
  printf '%s\n' "Ejecuta 'pnpm install' en $repo_dir y vuelve a intentar." >&2
  exit 1
fi

version=$(node -p "require(process.argv[1]).version" "$package_dir/package.json")
printf '%s\n' "[ok] pi-chrome $version instalado."
printf '%s\n' "Extension de Pi: $pi_extension"
printf '%s\n' "Extension de Chrome (Load unpacked): $browser_extension"

if [ "${1:-}" = "--open" ]; then
  if command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$browser_extension" | pbcopy
    printf '%s\n' "[ok] Ruta copiada al portapapeles."
  fi
  if command -v open >/dev/null 2>&1; then
    open "chrome://extensions" 2>/dev/null || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "chrome://extensions" >/dev/null 2>&1 || true
  fi
fi

printf '%s\n' "Activa Developer mode, pulsa Load unpacked y selecciona la ruta anterior."
printf '%s\n' "Despues habilita PI_CHROME_AUTO_AUTHORIZE=1 solo en un host de confianza."
