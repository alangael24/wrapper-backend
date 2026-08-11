# Wrapper Backend — OpenCode Go por usuario

Backend para tu wrapper: **cada usuario nuevo empieza en `free`, sin capacidad
de pago asignada**. Una suscripción de OpenCode Go solo se reclama después de
que un webhook de pago verificado o un administrador autenticado activa
`basic`/`pro`. El backend proxya las requests de LLM al upstream de Go con la
key asignada a ese usuario, registra uso y vigila los límites de la
suscripción. También puede ejecutar
tareas completas con **Pi** en modo RPC usando esa misma identidad y modelo.
Aunque DeepSeek V4 es de solo texto, el backend le añade visión mediante
**GPT-5.6 Luna**, con MiMo como fallback.

El backend base solo requiere Python y `cryptography`; Pi es una dependencia
opcional de Node.js y viene desactivado por defecto.

## Tiers de usuario

Hay 3 tiers; cada uno aplica un porcentaje de los límites de una suscripción
de OpenCode Go ($12 / 5h, $30 / semana, $60 / mes):

| Tier | Suscripción Go | Límites (5h / semana / mes) | Acceso a modelos |
|---|---|---|---|
| `free` | No se asigna | $0 / $0 / $0 | ❌ (402 `tier_requires_upgrade`) |
| `basic` | 50% | $6 / $15 / $30 | ✅ |
| `pro` | 100% | $12 / $30 / $60 | ✅ |

- `POST /v1/signup` siempre crea `free`. Cualquier `tier` enviado por el
  cliente se ignora y nunca consume una key del pool.
- `free` se crea **sin** suscripción y no puede llamar modelos.
- `basic` y `pro` necesitan una key disponible en el pool.
- Después de verificar el pago, el administrador puede cambiar el tier con
  `POST /admin/users/<id>/tier` `{"tier": "pro"}`: subir de tier asigna una
  suscripción del pool si no tiene una; bajarlo la libera de vuelta al pool.
- La activación y la reclamación de capacidad se ejecutan bajo
  `BEGIN IMMEDIATE`; un índice único parcial impide asociar una misma
  suscripción a dos usuarios.
- Los límites se reescalan según el tier: un usuario `basic` recibe 429 al
  llegar a $6 en 5h; uno `pro` al llegar a $12.

## Cómo funciona el modelo de suscripciones

OpenCode Go NO tiene una API pública para crear cuentas/suscripciones
programáticamente (la key se genera en `https://opencode.ai/auth` tras pagar;
solo un miembro por workspace puede suscribirse a Go). Por eso el backend
funciona con un **pool de suscripciones**:

1. El operador carga las keys de Go compradas al pool (una por usuario final).
2. Cada registro público (`POST /v1/signup`) crea un usuario `free` sin key.
3. Un webhook de Stripe con firma verificada —o, mientras se implementa, un
   administrador que ya comprobó el pago— activa `basic`/`pro`.
4. La transición pagada reclama una sola key del pool de forma atómica.
5. El usuario también puede traer su propia key (`POST /v1/byok`), pero el
   acceso sigue dependiendo del tier guardado por el servidor.
6. El backend proxya `chat/completions`, `responses` y `messages` al upstream
   con la key asignada, y registra el uso por ventanas.

El repositorio todavía no incluye el webhook de Stripe. No publiques un
checkout que prometa activación automática hasta añadir y verificar ese flujo;
el endpoint admin es la ruta segura provisional.

## Requisitos

- Python 3.12 (`python3.12` o el que tengas; si no, instálalo).
- `cryptography` (solo para cifrado AES; sin él usa el Keychain de macOS).
- Node.js y `pnpm` para habilitar el harness de Pi.

```bash
python3.12 -m venv .venv
.venv/bin/pip install cryptography
pnpm install                  # instala Pi 0.84.1 y pi-chrome 0.15.46
```

## Arranque rápido

```bash
./run.sh                     # lee .env si existe; sirve en 127.0.0.1:8787
# o manual:
ADMIN_TOKEN=mi-token .venv/bin/python -m go_backend.server serve --port 8787
```

## App de escritorio (Electron + TypeScript)

El repositorio incluye una interfaz de escritorio separada del backend y del
harness. Permite elegir conectores, buscar por herramienta, crear varios bots,
personalizar su color/forma/nombre y guardar qué conectores utilizará cada bot.
Después de crear un bot, una conversación guiada pregunta para qué se usará,
dónde vive el trabajo y qué sistema de proyectos debe considerar; con esas
respuestas recomienda y asigna conectores al perfil.

```bash
pnpm install
pnpm desktop
```

La app guarda preferencias y perfiles de bots en `desktop-state.json`, dentro
del directorio `userData` de Electron. Las sesiones de cuenta y de proveedores
se guardan aparte, cifradas con `safeStorage`/Keychain, con permisos `0600` y
ligadas al ID de la cuenta que inició sesión. Cerrar sesión borra también las
sesiones de proveedores para que otra persona del equipo no las herede. El
renderer no tiene acceso a Node.js, tokens ni red: toda autenticación pasa por
un `preload` aislado y una lista cerrada de operaciones IPC.

El catálogo reutiliza las superficies que ya existen en `outcome-desktop`
(trabajo, ventas, desarrollo y diseño) y `ecom-research-agent` (Shopify,
Tiendanube y WooCommerce). Electron ofrece conexión real y aislada por usuario
para 31 proveedores mediante el gateway administrado de Composio: Google
Workspace, Slack, Notion, LinkedIn, Zoom, GitHub, Jira, Linear, Asana, ClickUp,
Figma, Canva, Trello, monday.com, Intercom, Zendesk, Box, Dropbox, Calendly,
Stripe, QuickBooks, Greenhouse, Mailchimp, Shopify, Apollo, Ashby, Vercel, Hex,
Amplitude, Mixpanel y Databricks. Microsoft 365, HubSpot y Salesforce conservan
sus adaptadores OAuth directos. La primera conexión abre el
inicio de sesión de Agent Genia y después el consentimiento oficial del
proveedor; los tokens administrados permanecen en el servicio, nunca en el
renderer ni en Pi.

Los proveedores que exigen credenciales propias o no tienen un toolkit
compatible se muestran como `Próximamente`: seleccionarlos solo los asigna al
bot y no inventa una autenticación. El servicio se configura con
`OUTCOME_SERVICE_URL`, debe usar HTTPS fuera de loopback y guarda su
`COMPOSIO_API_KEY` exclusivamente en el entorno privado de producción.

El selector grande de herramientas aparece únicamente durante el onboarding
inicial. Después, el acceso `Plugins` abre un marketplace independiente con
búsqueda y las pestañas `Marketplace` y `Yours`. `Yours` se deriva de los IDs
instalados en `selectedConnectorIds`; desde ahí el usuario puede conectar,
desconectar o remover cada plugin sin volver al onboarding.

El marketplace incluye 49 proveedores distribuidos entre Trabajo, Ventas,
Soporte, Desarrollo, Diseño, Finanzas, RR. HH., Datos, Marketing y Comercio.
Además de las herramientas iniciales, incluye Trello, monday.com, Intercom,
Zendesk, Box, Dropbox, DocuSign, Calendly, Loom, Outreach, Salesloft, Apollo,
Clay, ZoomInfo, Nooks, Stripe, QuickBooks, NetSuite, Ramp, Workday, Rippling,
Ashby, Greenhouse, Vercel, Tableau, Hex, Amplitude, Mixpanel, Snowflake,
Databricks y Mailchimp. Que aparezcan en el catálogo no implica autenticación:
sin app OAuth y adaptador registrados se muestran como `Próximamente`.

La sesión OAuth de Electron y el adaptador del broker de Pi son límites de
confianza distintos. Conectar una cuenta en la interfaz prueba y conserva el
consentimiento real del usuario; para que una ejecución HTTP de Pi use esa
cuenta, el backend todavía necesita un adaptador del proveedor registrado para
ese mismo usuario. Pi nunca recibe refresh tokens ni client secrets.

Cargar keys de Go al pool:

```bash
# key(s) a mano
.venv/bin/python -m go_backend.server add-key sk-go-xxx sk-go-yyy

# desde stdin
echo "sk-go-xxx" | .venv/bin/python -m go_backend.server add-key -

# desde tu Keychain de macOS (el item que ya usa codex-opencode)
.venv/bin/python -m go_backend.server add-key --from-keychain
```

Probar el flujo:

```bash
curl -X POST http://127.0.0.1:8787/v1/signup \
  -H 'Content-Type: application/json' -d '{"name":"ana"}'
# -> { "api_key": "...", "tier": "free", "subscription_id": null, ... }

# Solo después de verificar el pago:
curl -X POST http://127.0.0.1:8787/admin/users/USER_ID/tier \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"tier":"pro"}'

curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer $API_KEY"
```

Para ejecutar una tarea con Pi, configura `PI_ENABLED=1`, reinicia el backend
y usa la api key del usuario:

```bash
curl http://127.0.0.1:8787/v1/agent/status \
  -H "Authorization: Bearer $API_KEY"

curl -X POST http://127.0.0.1:8787/v1/agent/run \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Revisa mis issues urgentes", "connector_ids":["github","linear"]}'
```

El recorrido es `cliente → agent/run → Pi RPC → chat/completions → OpenCode Go`.
Por eso las llamadas que hace Pi usan la suscripción asignada al
usuario y aparecen en `/v1/usage`. Cada ejecución tiene un workspace y logs
propios bajo `PI_RUNS_DIR`.

## Conectores nativos de Pi

Pi carga `extensions/connectors/index.ts` como una extensión first-party. Para
conservar el prompt cache y no mandar 18 esquemas de herramientas en cada turno,
al inicio solo está activa `connector_search`. Cuando el modelo busca una
capacidad, la extensión consulta el catálogo permitido para esa ejecución y
activa aditivamente únicamente la herramienta correspondiente, por ejemplo
`connector_github`.

El aislamiento es por ejecución:

1. El cliente envía `connector_ids` en `/v1/agent/run`; ids desconocidos se
   rechazan antes de arrancar Pi.
2. El backend crea un token aleatorio ligado al usuario y a esa lista cerrada.
3. El proceso de Pi recibe solo la URL loopback del broker y ese token; no recibe
   secretos OAuth, `ADMIN_TOKEN` ni la clave maestra del servidor.
4. Los endpoints internos aceptan únicamente tráfico loopback y limitan cada
   operación al grant. El token se revoca al terminar o fallar la tarea y además
   tiene una expiración máxima de una hora.
5. El adaptador del proveedor conserva y refresca sus credenciales dentro del
   backend. Si no existe o el usuario no inició sesión, la llamada falla cerrada
   con `connector_not_configured` o `connector_not_connected`.

La extensión y el broker no inventan una sesión OAuth: son la ruta segura entre
Pi y los adaptadores reales. Registrar las apps OAuth, callbacks y almacenamiento
cifrado sigue siendo obligatorio por proveedor. La selección visual de un bot
solamente determina el `connector_ids` que debe enviarse al ejecutar ese bot.

```bash
pnpm test:connectors
python3 -m unittest tests.test_backend -v
```

## Visión para DeepSeek

El puente multimodal viene activo por defecto para modelos cuyo nombre empieza
con `deepseek-v4` y funciona en `responses`, `chat/completions` y `messages`:

```text
imagen → Luna → reporte visual no confiable → DeepSeek V4 → respuesta/acciones
                  ↘ MiMo-V2.5 si Luna falla
```

- Luna recibe las imágenes con la misma suscripción Go asignada al usuario.
- DeepSeek recibe texto/OCR, estado de UI, defectos y evidencia relevante; no
  recibe los bytes de la imagen que no sabe interpretar.
- El consumo de Luna/MiMo y el de DeepSeek se registran como eventos separados.
- Los reportes se cachean por contenido de imagen **y prompt**, con límite LRU.
- Cada request admite como máximo 6 grupos y 12 imágenes para evitar ráfagas
  accidentales de llamadas visuales.
- `X-Wrapper-Vision-Model` indica qué modelo visual se usó.
- El reporte se marca explícitamente como evidencia no confiable para evitar
  que instrucciones escritas dentro de una imagen controlen al agente.

## Navegación con pi-chrome

`pi-chrome` está fijado en `package.json` y Pi carga automáticamente su extensión
desde `node_modules`. El backend **no usa ni acepta un perfil Chrome compartido**.
Para cada llamada con `browser:true` crea:

- un proceso Chrome separado;
- un `--user-data-dir` nuevo y vacío;
- una copia privada de la extensión companion;
- un puerto bridge local exclusivo.

El proceso y sus cookies se eliminan al terminar la ejecución. No cargues la
extensión companion manualmente en tu Chrome real. Para verificar la instalación:

```bash
pnpm install
./scripts/setup-pi-chrome.sh
```

Después:

1. Configura `PI_ENABLED=1`.
2. Mantén `PI_CHROME_ISOLATION=per_run` y, si no se autodetecta, define
   `PI_CHROME_BIN` con el ejecutable de Chrome for Testing o Chromium. Chrome
   estable no sirve: [desde v137 ignora `--load-extension`](https://developer.chrome.com/blog/extension-news-june-2025).
   Puedes instalar [Chrome for Testing](https://developer.chrome.com/blog/chrome-for-testing/)
   con `npx @puppeteer/browsers install chrome@stable`.
3. Configura `PI_CHROME_AUTO_AUTHORIZE=1`. Esta autorización solo cubre el
   perfil efímero de la ejecución actual.
4. Reinicia el backend y llama `/v1/agent/run` con `{"browser": true}`.

El servidor se niega a arrancar con `PI_CHROME_ISOLATION=shared` o cualquier
otro modo. Un perfil nuevo no contiene sesiones autenticadas: si una tarea debe
iniciar sesión, debe hacerlo dentro de esa ejecución y esos datos no se conservan.

Las capturas que produzca `pi-chrome` pasan por Luna antes de llegar a DeepSeek,
de modo que el agente puede observar la página y decidir su siguiente acción.

## Endpoints

Públicos (Bearer = api key del usuario del wrapper):

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/v1/signup` | Crea un usuario `free`; no acepta decisiones de tier ni asigna capacidad |
| POST | `/v1/byok` | El usuario registra su propia key de Go `{apiKey}` |
| GET | `/v1/models` | Catálogo de modelos (proxy a Go) |
| POST | `/v1/chat/completions` | Proxy OpenAI-compatible (stream y no-stream) |
| POST | `/v1/responses` | Proxy Responses API (stream y no-stream) |
| POST | `/v1/messages` | Proxy estilo Anthropic |
| GET | `/v1/usage` | Uso por ventanas con límites ajustados al tier |
| GET | `/v1/me` | Usuario, tier y suscripción asignada |
| GET | `/v1/agent/status` | Estado y capacidades habilitadas del harness de Pi |
| POST | `/v1/agent/run` | Ejecuta Pi con `{prompt, browser?: false, connector_ids?: string[]}` y espera el resultado |

Admin (Bearer = `ADMIN_TOKEN`):

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/admin/subscriptions` | Agregar keys de Go al pool `{keys:[...]}` |
| GET | `/admin/subscriptions` | Listar pool (keys cifradas, enmascaradas) |
| GET | `/admin/users` | Listar usuarios y asignaciones |
| POST | `/admin/users/<id>/revoke` | Devolver la suscripción al pool |
| POST | `/admin/users/<id>/tier` | Cambiar tier `{tier: "free"|"basic"|"pro"}` |
| GET | `/admin/usage` | Eventos de uso recientes |

## Uso y límites

- Los límites de Go son **$12 por 5 horas, $30 por semana, $60 por mes**.
- El tier del usuario escala esos límites (`basic` = 50%, `pro` = 100%).
- El backend estima el costo por request con la tabla de precios de
  `go_backend/go_prices.py` y responde `429 usage_limit` cuando el usuario
  alcanza un límite (configurable con `ENFORCE_LIMITS=0`).
- El uso se registra en `usage_events` con tokens input/output/cacheados y
  costo estimado. En streaming, el uso se captura del evento final si el
  upstream lo incluye (si no, se cuenta la request sin tokens).

## Configuración (env)

| Variable | Default | Descripción |
|---|---|---|
| `PORT` | `8787` | Puerto HTTP |
| `WRAPPER_SECRET` | auto | Clave maestra para cifrar keys Go |
| `ADMIN_TOKEN` | auto-generado | Token de admin; el valor publicado `cambia-este-token` impide arrancar |
| `DB_PATH` | `data/wrapper.sqlite` | Base de datos SQLite |
| `GO_BASE_URL` | `https://opencode.ai/zen/go/v1` | Upstream |
| `ENFORCE_LIMITS` | `1` | Rechazar al superar límites de Go |
| `VISION_ENABLED` | `1` | Añadir visión a los modelos objetivo de solo texto |
| `VISION_MODEL` | `gpt-5.6-luna` | Modelo primario de percepción visual |
| `VISION_FALLBACK_MODEL` | `mimo-v2.5` | Fallback visual; vacío lo desactiva |
| `VISION_TARGET_MODELS` | `deepseek-v4` | Prefijos de modelo separados por coma |
| `VISION_MAX_OUTPUT_TOKENS` | `2048` | Máximo del reporte de Luna |
| `VISION_FALLBACK_MAX_OUTPUT_TOKENS` | `4096` | Máximo del reporte fallback |
| `VISION_REASONING_EFFORT` | `minimal` | Esfuerzo de Luna para percepción |
| `VISION_REPORT_LIMIT` | `8000` | Caracteres máximos inyectados por reporte |
| `VISION_CACHE_ENTRIES` | `128` | Máximo de reportes visuales en caché LRU |
| `VISION_MAX_GROUPS` | `6` | Máximo de grupos visuales por request |
| `VISION_MAX_IMAGES` | `12` | Máximo de imágenes únicas por request |
| `PI_ENABLED` | `0` | Habilitar el endpoint de tareas de Pi |
| `PI_BIN` | `./node_modules/.bin/pi` | Ejecutable de Pi |
| `PI_NODE_BIN_DIR` | vacío | Directorio de `node` si no está en PATH |
| `PI_BACKEND_URL` | `http://127.0.0.1:$PORT` | URL que Pi usa para volver al wrapper |
| `PI_RUNS_DIR` | `data/pi-runs` | Workspaces y logs por ejecución |
| `PI_MODEL` | `deepseek-v4-flash` | Modelo configurado en Pi |
| `PI_THINKING` | `high` | Nivel de razonamiento de Pi |
| `PI_TIMEOUT_SECONDS` | `1800` | Timeout; `0` significa sin límite |
| `PI_MAX_CONCURRENT` | `2` | Procesos Pi simultáneos |
| `PI_MAX_PROMPT_CHARS` | `100000` | Tamaño máximo del prompt |
| `PI_CONNECTOR_EXTENSION` | `./extensions/connectors/index.ts` | Extensión first-party con `connector_search` y herramientas diferidas |
| `PI_CONNECTOR_TOKEN_TTL_SECONDS` | timeout + 60, máx. 3600 | Vida máxima del grant interno por ejecución |
| `PI_CHROME_EXTENSION` | `./node_modules/pi-chrome/.../index.ts` | Extensión Pi de pi-chrome |
| `PI_CHROME_BIN` | autodetectado | Ejecutable de Chrome for Testing/Chromium; no es una ruta de perfil |
| `PI_CHROME_ISOLATION` | `per_run` | Único modo válido: proceso, perfil y bridge nuevos por ejecución |
| `PI_CHROME_AUTO_AUTHORIZE` | `0` | Autorizar automáticamente solo el Chrome efímero de esa ejecución |
| `PI_CHROME_AUTHORIZE_MINUTES` | `30` | Duración máxima; el proceso se cierra antes si termina la tarea |

## Seguridad

- Las keys de Go se cifran con AES-256-GCM (`cryptography`) o se guardan en
  el Keychain de macOS (fallback). Nunca se persisten en claro.
- Las api keys de los usuarios del wrapper se guardan hasheadas (SHA-256);
  solo se muestran una vez en el signup.
- El signup público siempre crea `free`. Solo una transición autenticada tras
  comprobar el pago puede activar `basic`/`pro` y reclamar capacidad.
- La asignación de suscripciones usa `BEGIN IMMEDIATE` y el índice único
  `uniq_user_subscription`; dos activaciones concurrentes no pueden compartir
  una key.
- El servidor se niega a arrancar con `ADMIN_TOKEN=cambia-este-token`. En
  producción define un token aleatorio largo y mantenlo fuera del repositorio.
- Los secretos viven en `data/secret.key` (0600); si usas `WRAPPER_SECRET`
  en producción, bórralo y apóyalo en tu gestor de secretos.
- Pi puede ejecutar comandos y manipular archivos con los permisos del proceso
  del backend. El endpoint viene desactivado; en producción debe correr en un
  contenedor o sandbox por tarea, sin montar secretos ni el código del servidor.
- El subproceso de Pi recibe un entorno limpio: no hereda `ADMIN_TOKEN`,
  `WRAPPER_SECRET` ni las demás API keys del servidor.
- `pi-chrome` nunca recibe el perfil real del operador. Cada ejecución obtiene
  un perfil vacío y un bridge loopback propio; ambos procesos se terminan y el
  perfil se borra al finalizar. El arranque rechaza explícitamente el modo
  compartido.
- El aislamiento de perfil evita compartir cookies, sesiones y almacenamiento
  entre clientes. Pi todavía ejecuta con el usuario del sistema del backend;
  para aislamiento fuerte entre tenants usa además un contenedor o usuario del
  sistema distinto por tarea.

## Tests

```bash
.venv/bin/python tests/test_backend.py
```

Corren contra un upstream mock (sin llamadas reales a OpenCode Go) y cubren:
signup siempre-free, activación admin, carrera de asignación, rechazo del token
inseguro, proxy de modelos, chat/responses/messages, streaming,
registro de uso, límite 429, tiers (free/basic/pro), BYOK, revocación y
cifrado en reposo. También validan el flujo Pi RPC completo con un ejecutable
falso, el puente Luna/MiMo, los grants efímeros del broker, la carga dinámica de
herramientas y el aislamiento de proceso, perfil y bridge de pi-chrome, sin
consumir saldo real.
