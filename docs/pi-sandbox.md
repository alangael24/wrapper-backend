# Sandbox de Pi para Agent Genia

Este sandbox se integra como un ejecutable compatible con `PI_BIN`. No cambia
`go_backend/pi_harness.py`: el harness continúa creando su run efímero, pasando
los mismos argumentos RPC y controlando timeout, concurrencia, logs, Chrome y
conectores. El launcher crea la frontera de seguridad justo antes de ejecutar el
binario real de Pi.

```text
PiHarness
  └── scripts/pi-sandbox                 proceso padre, conserva credenciales
        ├── proxy HTTP de capacidades    allowlist + inyección de headers
        ├── relay raw de Chrome          solo cuando el harness lo delegó
        └── bubblewrap
              └── entrypoint + socat
                    └── Pi y todos sus descendientes
```

## Qué tomamos de Codex, Claude y Cursor

La investigación se hizo contra documentación y repositorios primarios:

| Runtime | Patrón relevante | Aplicación aquí |
|---|---|---|
| OpenAI Codex | Restricciones aplicadas por el sistema operativo, escritura limitada al workspace, red cerrada y egress administrado | Namespaces/mounts de Bubblewrap; red `deny` y capacidades loopback cerradas |
| Claude Code | Bubblewrap + `socat`, proxy de red, sentinels para credenciales y política fail-closed | Mismo patrón general, adaptado al backend local y a los tokens efímeros del harness |
| Cursor | Landlock/seccomp en Linux, overlay de filesystem y revisión adicional de acciones | Política de filesystem por allowlist; seccomp propio queda como hardening posterior, no como sustituto de la frontera base |

Fuentes primarias:

- OpenAI, [Building a safe, effective sandbox to enable Codex on Windows](https://openai.com/index/building-codex-windows-sandbox/), [Running Codex safely at OpenAI](https://openai.com/index/running-codex-safely/) y [Introducing upgrades to Codex](https://openai.com/index/introducing-upgrades-to-codex/).
- Anthropic, [Configure the sandboxed Bash tool](https://code.claude.com/docs/en/sandboxing) y [`anthropic-experimental/sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime).
- Cursor, [Implementing a secure sandbox for local agents](https://cursor.com/blog/agent-sandboxing) y [Auto-review: Catching security issues before they ship](https://cursor.com/blog/auto-review).

No se copió código de esos runtimes. Se reutilizaron principios de diseño y se
construyó una política específica para el contrato de este repositorio.

## Frontera implementada

### Filesystem

Bubblewrap construye una raíz nueva. El launcher monta:

- `/usr`, librerías, certificados y archivos mínimos de identidad del sistema,
  solo lectura;
- `node_modules` y `extensions/`, solo lectura;
- `workspace`, `home` y `config` del run, lectura/escritura;
- `/tmp` como `tmpfs` privado de 512 MiB;
- un directorio de sockets Unix y el entrypoint, solo lectura.

La raíz vacía de Bubblewrap se remonta de solo lectura al terminar de construir
los mounts. Los submounts declarados para `workspace`, `home`, `config` y `/tmp`
conservan escritura; no queda un segundo árbol efímero escribible fuera de la
política.

No se monta `/` del host. El repositorio completo, `.env`, bases de datos, logs
del harness, perfiles reales, SSH agent y sockets de Docker/Podman/containerd no
son visibles. Los procesos descendientes heredan la misma frontera.

Las extensiones de Pi solo pueden resolverse dentro de `node_modules` o
`extensions/`. Una ruta externa cancela el run; no se monta su directorio padre.
Los ejecutables requeridos tampoco pueden vivir dentro del run escribible ni ser
escribibles por grupo u otros.

### Red

`--unshare-net` crea un namespace sin egress. Dentro del sandbox solo existen
listeners `127.0.0.1` iniciados por `socat`. Cada listener conecta a un socket
Unix host-side y representa una capacidad concreta:

1. API de modelos: solo `models`, `chat/completions`, `responses` y `messages`
   bajo el prefijo configurado.
2. Broker: solo rutas internas de conectores y computadora.
3. Bridge de Chrome: relay TCP al puerto efímero reservado por el harness, solo
   cuando el run habilitó browser.

No hay DNS ni acceso arbitrario a internet, metadata cloud o puertos del host.
Las URLs del backend y broker deben ser HTTP loopback, usar puertos no
privilegiados y no contener credenciales, query, fragment, percent-encoding,
backslashes ni dot-segments.

El proxy limita el body a 16 MiB, acepta solo métodos/rutas declarados, usa una
allowlist pequeña de headers y limita el fanout host-side a 64 conexiones por
capacidad. También rechaza respuestas comprimidas para poder inspeccionar y
enmascarar secretos de forma determinista.

### Credenciales

El proceso padre conserva `WRAPPER_PI_API_KEY` y `PI_CONNECTOR_RUN_TOKEN`. El
env del sandbox recibe sentinels aleatorios de exactamente la misma longitud.
El proxy HTTP:

- descarta `Authorization` y `X-Connector-Run-Token` enviados por el proceso
  aislado;
- inyecta la credencial real únicamente en la ruta autorizada;
- deja intacto el request body, aunque contenga el sentinel;
- vuelve a enmascarar la credencial en headers y body si el upstream la refleja;
- no escribe argumentos ni sentinels en el audit log.

Pi, las herramientas shell y los procesos hijos nunca necesitan poseer el
secreto real. La credencial cruza solo el socket host-side y la conexión
loopback al backend.

### Procesos y recursos

Se crean namespaces de usuario, PID, UTS, IPC y red; cgroup se separa cuando el
kernel lo permite. Todas las Linux capabilities se eliminan y Bubblewrap instala
la protección que niega user namespaces anidados. Esta versión exige Bubblewrap
sin bit setuid y las opciones de seguridad modernas; una instalación incompatible
falla en preflight.

`prlimit` es obligatorio y aplica:

- core dumps: 0;
- archivos abiertos: 1024;
- procesos: 256;
- tamaño máximo por archivo: 1 GiB.

`/tmp` tiene 512 MiB. El timeout y `PI_MAX_CONCURRENT` siguen perteneciendo al
harness. Para límites duros de memoria, CPU y disco acumulado, ejecuta el backend
en un cgroup/container con cuotas; Bubblewrap no reemplaza esas cuotas.

### Fallo cerrado

No existe modo degradado ni retry sin sandbox. Si faltan `bubblewrap`, `socat`,
`prlimit`, Node, Pi, user namespaces o una opción requerida de Bubblewrap, el
launcher devuelve código 78 y Pi no se ejecuta. Esto es intencional: el harness
corre sin interacción humana para aprobar una degradación.

## Instalación

Linux es el único backend soportado por esta primera versión.

```bash
# Debian/Ubuntu
sudo apt-get update
sudo apt-get install -y bubblewrap socat util-linux

pnpm install
./scripts/setup-pi-sandbox.sh
```

En Fedora/RHEL:

```bash
sudo dnf install bubblewrap socat util-linux
```

En Arch:

```bash
sudo pacman -S bubblewrap socat util-linux
```

Ubuntu 24.04 o un host endurecido puede bloquear user namespaces mediante
AppArmor. `./scripts/setup-pi-sandbox.sh` ejecuta un probe real y muestra el
error del kernel. Crea una política AppArmor específica para `bwrap`; no
desactives globalmente las defensas del host en un servicio multiusuario.

## Activación

`.env.example` ya apunta al launcher:

```dotenv
PI_ENABLED=1
PI_BIN=./scripts/pi-sandbox
PI_BACKEND_URL=http://127.0.0.1:8787
```

Tanto `run.sh` como la configuración Python usan `./scripts/pi-sandbox` cuando
Pi está habilitado y `PI_BIN` no fue definido. `run.sh` además ejecuta `--check`
antes de levantar el servidor. Un arranque manual también falla cerrado al
primer run si el host Linux no satisface el preflight:

```bash
PI_ENABLED=1 \
PI_BIN=./scripts/pi-sandbox \
ADMIN_TOKEN='...' \
.venv/bin/python -m go_backend.server serve --port 8787
```

No habilites Pi en macOS o Windows con esta versión. Desarrolla allí, pero sirve
las ejecuciones de agentes desde un host Linux que supere el preflight.

## Auditoría

Cada run conserva:

```text
data/pi-runs/<run_id>/sandbox-audit.json
```

El archivo registra mounts, capacidades, límites, PID, estado y hash de los
argumentos de Pi. No contiene tokens reales, sentinels ni argumentos en claro.
El directorio temporal de sockets se elimina al finalizar.

Comandos útiles:

```bash
# Probe real de kernel + Bubblewrap
./scripts/pi-sandbox --check

# Resumen estático de política
./scripts/pi-sandbox --policy

# Unit tests del launcher/proxies
python -m unittest tests.test_pi_sandbox -v

# Suite existente: valida que el contrato del harness siga igual
python -m unittest tests.test_backend -v
```

Pruebas negativas recomendadas en staging:

```text
1. pedir al agente leer /etc/shadow, .env o la base de datos;
2. pedirle escribir fuera de workspace/home/config;
3. ejecutar curl contra internet, metadata cloud o un puerto loopback no delegado;
4. imprimir WRAPPER_PI_API_KEY y PI_CONNECTOR_RUN_TOKEN;
5. montar o abrir docker.sock, SSH_AUTH_SOCK o /proc del host;
6. lanzar un proceso hijo y repetir las mismas pruebas.
```

Todas deben fallar o mostrar únicamente sentinels.

## Alcance y límites conocidos

- Es aislamiento de procesos sobre el mismo kernel, no una VM ni microVM.
- Chrome lo inicia el harness fuera del namespace de Pi. Su perfil, extensión y
  bridge son efímeros por run, pero el navegador es una capacidad de egress
  deliberada y debe habilitarse solo para bots autorizados.
- La computadora persistente Daytona tiene otra frontera y no se ejecuta dentro
  de este Bubblewrap.
- No se instaló un filtro seccomp propio para todo Pi. Bubblewrap sí niega user
  namespaces anidados; un filtro BPF adicional debe versionarse y probarse contra
  Node/Pi antes de activarlo para no crear un falso sentido de seguridad.
- Ejecuta el backend como usuario dedicado, conserva código/dependencias de solo
  lectura para ese usuario y aplica cgroups/quotas desde systemd, Kubernetes o el
  runtime de despliegue.
