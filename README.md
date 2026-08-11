# Wrapper Backend — OpenCode Go por usuario

Backend para tu wrapper: **cada usuario nuevo recibe automáticamente una
suscripción de OpenCode Go asignada** (una key por usuario). El backend
proxya las requests de LLM al upstream de Go con la key de ese usuario,
registra uso y vigila los límites de la suscripción. También puede ejecutar
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

- En el signup se elige el tier con `{"tier": "basic"}` (default: `basic`).
- `free` se crea **sin** suscripción y no puede llamar modelos.
- `basic` y `pro` necesitan una key disponible en el pool (si no hay, el
  signup responde `409 no_subscriptions_available`).
- El administrador puede cambiar el tier de un usuario con
  `POST /admin/users/<id>/tier` `{"tier": "pro"}`: subir de tier asigna una
  suscripción del pool si no tiene una; bajarlo la libera de vuelta al pool.
- Los límites se reescalan según el tier: un usuario `basic` recibe 429 al
  llegar a $6 en 5h; uno `pro` al llegar a $12.

## Cómo funciona el modelo de suscripciones

OpenCode Go NO tiene una API pública para crear cuentas/suscripciones
programáticamente (la key se genera en `https://opencode.ai/auth` tras pagar;
solo un miembro por workspace puede suscribirse a Go). Por eso el backend
funciona con un **pool de suscripciones**:

1. El operador carga las keys de Go compradas al pool (una por usuario final).
2. Cada usuario nuevo que se registra (`POST /v1/signup`) recibe
   **automáticamente** una key disponible del pool (1:1, sin intervención).
3. El usuario también puede traer su propia key (`POST /v1/byok`).
4. El backend proxya `chat/completions`, `responses` y `messages` al upstream
   con la key asignada, y registra el uso por ventanas.

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
# -> { "api_key": "...", "subscription_id": "sub_...", ... }  (guárdalo)

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
  -d '{"prompt":"Investiga tres proveedores y compara precio, MOQ y riesgos"}'
```

El recorrido es `cliente → agent/run → Pi RPC → chat/completions → OpenCode Go`.
Por eso las llamadas que hace Pi usan la suscripción asignada al
usuario y aparecen en `/v1/usage`. Cada ejecución tiene un workspace y logs
propios bajo `PI_RUNS_DIR`.

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
desde `node_modules`. Chrome requiere una instalación manual única de la
extensión companion unpacked, porque usa el perfil real ya autenticado:

```bash
pnpm install
./scripts/setup-pi-chrome.sh --open
```

El script copia la ruta correcta y abre `chrome://extensions`. Activa
**Developer mode**, elige **Load unpacked** y pega la ruta mostrada. Después:

1. Configura `PI_ENABLED=1`.
2. Configura `PI_CHROME_AUTO_AUTHORIZE=1` únicamente en una máquina y perfil
   Chrome dedicados y de confianza.
3. Reinicia el backend y llama `/v1/agent/run` con `{"browser": true}`.

Las capturas que produzca `pi-chrome` pasan por Luna antes de llegar a DeepSeek,
de modo que el agente puede observar la página y decidir su siguiente acción.

## Endpoints

Públicos (Bearer = api key del usuario del wrapper):

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/v1/signup` | Crea usuario (tier opcional) y le asigna una suscripción Go del pool si aplica |
| POST | `/v1/byok` | El usuario registra su propia key de Go `{apiKey}` |
| GET | `/v1/models` | Catálogo de modelos (proxy a Go) |
| POST | `/v1/chat/completions` | Proxy OpenAI-compatible (stream y no-stream) |
| POST | `/v1/responses` | Proxy Responses API (stream y no-stream) |
| POST | `/v1/messages` | Proxy estilo Anthropic |
| GET | `/v1/usage` | Uso por ventanas con límites ajustados al tier |
| GET | `/v1/me` | Usuario, tier y suscripción asignada |
| GET | `/v1/agent/status` | Estado y capacidades habilitadas del harness de Pi |
| POST | `/v1/agent/run` | Ejecuta Pi con `{prompt, browser?: false}` y espera el resultado |

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
| `ADMIN_TOKEN` | auto-generado | Token de los endpoints admin |
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
| `PI_CHROME_EXTENSION` | `./node_modules/pi-chrome/.../index.ts` | Extensión Pi de pi-chrome |
| `PI_CHROME_AUTO_AUTHORIZE` | `0` | Permitir autorización automática de Chrome |
| `PI_CHROME_AUTHORIZE_MINUTES` | `30` | Duración de la autorización de Chrome |

## Seguridad

- Las keys de Go se cifran con AES-256-GCM (`cryptography`) o se guardan en
  el Keychain de macOS (fallback). Nunca se persisten en claro.
- Las api keys de los usuarios del wrapper se guardan hasheadas (SHA-256);
  solo se muestran una vez en el signup.
- Los secretos viven en `data/secret.key` (0600); si usas `WRAPPER_SECRET`
  en producción, bórralo y apóyalo en tu gestor de secretos.
- Pi puede ejecutar comandos y manipular archivos con los permisos del proceso
  del backend. El endpoint viene desactivado; en producción debe correr en un
  contenedor o sandbox por tarea, sin montar secretos ni el código del servidor.
- El subproceso de Pi recibe un entorno limpio: no hereda `ADMIN_TOKEN`,
  `WRAPPER_SECRET` ni las demás API keys del servidor.
- `pi-chrome` controla un perfil Chrome real con permisos amplios. Su bridge se
  limita a `127.0.0.1:17318`, pero otros procesos locales del mismo usuario son
  parte de su superficie de confianza.
- Activar `PI_CHROME_AUTO_AUTHORIZE=1` permite que cualquier usuario con acceso
  válido a `agent/run` y `browser:true` controle ese perfil. No lo actives en un
  backend multiusuario público; usa un host/perfil dedicado o una capa adicional
  de autorización.

## Tests

```bash
.venv/bin/python tests/test_backend.py
```

Corren contra un upstream mock (sin llamadas reales a OpenCode Go) y cubren:
signup/asignación, proxy de modelos, chat/responses/messages, streaming,
registro de uso, límite 429, tiers (free/basic/pro), BYOK, revocación y
cifrado en reposo. También validan el flujo Pi RPC completo con un ejecutable
falso, el puente Luna/MiMo y la carga/autorización de pi-chrome, sin consumir
saldo real.
