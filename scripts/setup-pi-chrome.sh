#!/bin/sh
# Verifica el runtime aislado de pi-chrome. No modifica ningun perfil real.
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

node_bin=
if [ -n "${PI_NODE_BIN_DIR:-}" ] && [ -x "$PI_NODE_BIN_DIR/node" ]; then
  node_bin=$PI_NODE_BIN_DIR/node
elif command -v node >/dev/null 2>&1; then
  node_bin=$(command -v node)
else
  printf '%s\n' "[error] No se encontro node. Configura PI_NODE_BIN_DIR." >&2
  exit 1
fi

version=$("$node_bin" -p "require(process.argv[1]).version" "$package_dir/package.json")
printf '%s\n' "[ok] pi-chrome $version instalado."
printf '%s\n' "Extension de Pi: $pi_extension"

if [ "${1:-}" = "--open" ]; then
  printf '%s\n' "[error] --open fue eliminado: no cargues pi-chrome en un perfil real." >&2
  exit 2
fi

chrome_bin=${PI_CHROME_BIN:-}
if [ -n "$chrome_bin" ] && [ ! -x "$chrome_bin" ] && command -v "$chrome_bin" >/dev/null 2>&1; then
  chrome_bin=$(command -v "$chrome_bin")
fi
if [ -z "$chrome_bin" ]; then
  for candidate in \
    "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium"
  do
    if [ -x "$candidate" ]; then
      chrome_bin=$candidate
      break
    fi
  done
fi
if [ -z "$chrome_bin" ]; then
  for command_name in chromium chromium-browser
  do
    if command -v "$command_name" >/dev/null 2>&1; then
      chrome_bin=$(command -v "$command_name")
      break
    fi
  done
fi
case "$chrome_bin" in
  *"/Google Chrome.app/"*|*/google-chrome|*/google-chrome-stable)
    printf '%s\n' "[error] Chrome estable no admite --load-extension desde v137." >&2
    printf '%s\n' "Instala Chrome for Testing o Chromium y configura PI_CHROME_BIN." >&2
    exit 1
    ;;
esac
if [ -z "$chrome_bin" ] || [ ! -x "$chrome_bin" ]; then
  printf '%s\n' "[error] No se encontro Chrome for Testing/Chromium compatible." >&2
  printf '%s\n' "Instalacion oficial: npx @puppeteer/browsers install chrome@stable" >&2
  printf '%s\n' "Despues configura PI_CHROME_BIN con la ruta que imprime el comando." >&2
  exit 1
fi

printf '%s\n' "[ok] Chrome: $chrome_bin"
printf '%s\n' "[ok] El backend creara un perfil y un bridge efimeros por ejecucion."
printf '%s\n' "Configura PI_CHROME_ISOLATION=per_run; nunca cargues esta extension en tu Chrome real."
