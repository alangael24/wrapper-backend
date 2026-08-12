# Agent Genia — backend y runtime de agentes

Backend de Agent Genia: **cada usuario nuevo empieza en `free`, sin capacidad
de pago asignada**. La capacidad administrada solo se reclama después de que un
webhook de pago verificado o un administrador autenticado activa `basic`/`pro`.
El backend envía las requests de LLM al proveedor configurado con la credencial
asignada a ese usuario, registra uso y vigila los límites de la suscripción.
También puede ejecutar
tareas completas con **Pi** en modo RPC usando esa misma identidad y modelo.
Aunque DeepSeek V4 es de solo texto, el backend le añade visión mediante
**GPT-5.6 Luna**, con MiMo como fallback.

El backend base requiere Python, `cryptography` y `psycopg` con su pool cuando usa
Supabase/Postgres; Pi es una dependencia opcional de Node.js y viene
desactivado por defecto.

## Tiers de usuario

Hay 3 tiers; cada uno aplica un porcentaje de los presupuestos internos de uso
($12 / 5h, $30 / semana, $60 / mes):

| Tier | Capacidad administrada | Límites (5h / semana / mes) | Acceso a modelos |
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

## Cómo funciona el modelo de capacidad

Las credenciales del proveedor de modelos se administran exclusivamente en el
servidor mediante un **pool de capacidad**:

1. El operador carga las credenciales del proveedor al pool.
2. Cada registro público (`POST /v1/signup`) crea un usuario `free` sin key.
3. Stripe Checkout cobra un price ID fijo elegido por el servidor; un webhook
   con firma verificada activa `basic`/`pro` después del pago.
4. La transición pagada reclama una sola key del pool de forma atómica.
5. El usuario también puede traer su propia key (`POST /v1/byok`), pero el
   acceso sigue dependiendo del tier guardado por el servidor.
6. El backend proxya `chat/completions`, `responses` y `messages` al upstream
   con la key asignada, y registra el uso por ventanas.

### Persistencia con PostgreSQL/Supabase

La migración incluida crea el esquema privado `agentgenia`. Las tablas no se
exponen al Data API, no otorgan permisos a `anon`/`authenticated` y tienen RLS
habilitado como defensa adicional. El backend se conecta exclusivamente con
`DATABASE_URL`, que debe guardarse como secreto del host. Desarrollo y pruebas
usan SQLite cuando esa variable está vacía.

Las migraciones versionadas viven en `supabase/migrations/`. Las transiciones de
tier, el procesamiento idempotente de webhooks y la reclamación de capacidad se
ejecutan dentro de transacciones. Los índices únicos parciales son la defensa
final que impide asignar una misma suscripción a dos usuarios, incluso ante una
carrera.

En cualquier host con filesystem efímero define al menos `DATABASE_URL`,
`WRAPPER_SECRET` y `ADMIN_TOKEN` como secretos. No dependas de `DB_PATH` ni de
`secret.key` como estado persistente. Los directorios de ejecución del harness
son temporales y no forman parte de la base de datos.

### Stripe Checkout en producción

La integración usa Checkout alojado, el Customer Portal y webhooks idempotentes.
Electron nunca recibe la secret key, el webhook secret ni un price ID arbitrario.
El cliente solo solicita `basic` (Plus) o `pro`; el backend resuelve el price ID
live configurado y el tier no cambia hasta recibir un evento firmado.

1. Aplica las migraciones de Supabase, incluida
   `20260812233100_stripe_event_ordering.sql`, antes de desplegar esta versión
   del backend.
2. Configura las variables `STRIPE_*` de `.env.example` en el gestor de secretos
   del host y establece `STRIPE_ENABLED=1`.
3. Registra `https://TU-BACKEND/v1/billing/webhook` como destino de eventos live.
4. Suscribe al menos `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed` e
   `invoice.payment_action_required`.
5. Copia el signing secret `whsec_...` al entorno privado como
   `STRIPE_WEBHOOK_SECRET`.

Los estados `active` y `trialing` activan acceso. `past_due` conserva el acceso
durante los reintentos de Stripe; `unpaid`, `canceled`, `paused` e
`incomplete_expired` vuelven a `free` y liberan la capacidad. Si el pool no tiene
una key disponible, la transacción se revierte y Stripe reintenta el webhook.

## Requisitos

- Python 3.12 (`python3.12` o el que tengas; si no, instálalo).
- `cryptography` para cifrado AES y `psycopg[binary,pool]` para Supabase/Postgres.
- Node.js y `pnpm` para habilitar el harness de Pi.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
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
Después de crear un bot, el modelo genera el saludo y, cuando ayuda, un widget
genérico de pregunta con entre una y seis opciones. La interfaz solo valida y
renderiza ese schema: las preguntas, opciones y respuestas no están
hardcodeadas ni forman parte del onboarding de la aplicación.

```bash
pnpm install
pnpm desktop

# paquete sin publicar para la plataforma actual
pnpm pack:desktop
```

La distribución usa Electron Builder: macOS genera DMG/PKG/ZIP universal,
Windows NSIS EXE y AppX/MSIX para Store o MDM, y Linux AppImage/deb/rpm. Las
releases firmadas nacen de tags `desktop-vX.Y.Z`, publican checksums y alimentan
el actualizador interno. Consulta [docs/distribution.md](docs/distribution.md)
antes de crear un tag.

## App de iOS (SwiftUI)

La app nativa para iPhone y iPad vive en `ios/AgentGenia`. Usa el mismo
`wrapper-backend` de producción y no contiene una copia del harness de Pi. El
cliente llama a los contratos públicos de cuenta, agentes, conectores, billing
y computadoras; por lo tanto, el modelo, las herramientas y sus límites siguen
controlados por el servidor.

```bash
open ios/AgentGenia/AgentGenia.xcodeproj

# comprobación reproducible sin firma
xcodebuild \
  -project ios/AgentGenia/AgentGenia.xcodeproj \
  -scheme AgentGenia \
  -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO build
```

La configuración Release apunta a `https://agentgenia-api.onrender.com`. Para
desarrollo se puede definir `AGENTGENIA_API_BASE_URL` en el Scheme de Xcode; la
app solo admite HTTPS, excepto loopback local. El bundle es
`com.agentgenia.ios` y la configuración de firma usa el equipo de Apple
Developer del proyecto.

El login reutiliza Google Authorization Code + PKCE del backend. La app abre la
autorización en una hoja segura y consulta el intento ligado a un UUID de
dispositivo. Access token y refresh token se guardan en Keychain con protección
`AfterFirstUnlockThisDeviceOnly`; los tokens OAuth de plugins nunca llegan a
iOS. Cada cuenta guarda sus bots en un archivo distinto bajo Application
Support, protegido por iOS, de modo que cerrar sesión no mezcla conversaciones.

El chat llama realmente a `/v1/agent/run` y conserva los widgets de preguntas
generados por el LLM, sin saludos ni opciones hardcodeadas. El marketplace
muestra el catálogo completo, autoriza cuentas mediante el mismo gateway de
Composio/adaptadores first-party y su pestaña `Tuyos` contiene los plugins que
el usuario instaló, estén conectados o todavía pendientes de autorización. La
computadora de cada bot usa los endpoints
`/v1/computers/*` y presenta el viewer firmado en un `WKWebView` efímero que
rechaza navegación a otros orígenes.

Stripe se abre fuera del contexto del agente y la app nunca incluye secret
keys. La build Debug puede probar Checkout/Portal; la build Release para App
Store oculta compras y enlaces externos de pago. Los usuarios conservan acceso
al plan adquirido en web. Una venta móvil futura requiere StoreKit y validación
server-side antes de volver a habilitar compras dentro de iOS.

## App de Android (Kotlin + Jetpack Compose)

La app nativa para teléfonos y tablets Android vive en `android/AgentGenia`.
Comparte directamente los contratos públicos de `wrapper-backend` con Electron
y iOS; no copia ni modifica el harness de Pi. Incluye login Google ligado al
dispositivo, chat real con widgets generados por el LLM, marketplace y pestaña
`Tuyos`, estado de billing y el viewer firmado de la computadora persistente de
cada bot.

```bash
cd android/AgentGenia
./gradlew testDebugUnitTest assembleDebug

# APK de desarrollo
open app/build/outputs/apk/debug
```

Para compilar se necesita JDK 17 y Android SDK Platform 37 con Build Tools
36.0.0; Android Studio instala y administra esas dependencias. El Gradle
Wrapper 9.5 queda incluido en el repositorio para que CI y Windows usen la
misma versión.

La build usa Kotlin, Jetpack Compose y `minSdk 26`. Los access/refresh tokens y
los archivos de bots se cifran con AES-GCM usando una clave no exportable de
Android Keystore; el estado se guarda por hash de cuenta en `noBackupFilesDir`
y se excluye de cloud backup y device transfer. Los secretos OAuth de cada
proveedor permanecen en el backend/Composio y nunca llegan al teléfono.

La configuración predeterminada apunta a
`https://agentgenia-api.onrender.com`. Para una variante interna se puede
reemplazar `API_BASE_URL` en `app/build.gradle.kts`; el cliente solo admite
HTTPS, salvo un loopback explícito para desarrollo. El WebView de la
computadora desactiva acceso a archivos, contenido local, mixed content,
ventanas adicionales y navegación fuera del host firmado.

La build Release de Google Play no muestra Checkout, Portal ni enlaces externos
de pago. Los planes comprados en web siguen funcionando al iniciar sesión. Una
venta móvil futura requiere Google Play Billing y verificación server-side. El
workflow de release genera y verifica el AAB firmado, lo carga al track elegido y
conserva checksum/provenance; Data Safety se documenta en
[docs/store-submission.md](docs/store-submission.md).

Las tareas `bundleRelease` y `assembleRelease` se niegan a terminar sin una
firma de producción. Define estas variables como secretos del entorno o como
propiedades privadas de Gradle; nunca las agregues al repositorio:

```bash
export AGENTGENIA_RELEASE_STORE_FILE=/ruta/privada/agentgenia-upload.jks
export AGENTGENIA_RELEASE_STORE_PASSWORD='...'
export AGENTGENIA_RELEASE_KEY_ALIAS='agentgenia-upload'
export AGENTGENIA_RELEASE_KEY_PASSWORD='...'

cd android/AgentGenia
./gradlew clean testDebugUnitTest lintDebug assembleDebug bundleRelease
# AAB firmado: app/build/outputs/bundle/release/app-release.aab
```

### Estado local y seguridad de Electron

Electron guarda preferencias y perfiles de bots en un archivo aislado por cuenta
dentro de `userData/accounts` de Electron. Al cerrar sesión carga un estado
vacío en memoria y no expone los bots de la cuenta anterior. Una instalación
existente migra una sola vez su antiguo `desktop-state.json` a la cuenta que ya
estaba autenticada y retira el archivo compartido del flujo normal. Solo la
sesión opaca de Agent Genia se
guarda cifrada con `safeStorage`/Keychain y permisos `0600`. Los tokens de cada
proveedor permanecen en Composio bajo el `user_id` autenticado y nunca entran a
Electron ni a Pi. El renderer no tiene acceso a Node.js, tokens ni red: toda
autenticación pasa por un `preload` aislado y una lista cerrada de operaciones
IPC.

### Teach a task

Cada bot ofrece `Teach a task` en el top bar y en el menú `+` del composer. La
app abre el selector nativo de pantalla/ventana, reserva una sola grabación a la
vez y muestra un overlay con cronómetro, `Stop & save` y `Discard`. La grabación
no incluye audio, dura como máximo cinco minutos y se limita a 64 MB.

Al guardar, Electron conserva el video con permisos `0600` dentro de
`userData/teach-recordings/<hash-de-cuenta>` y envía hasta doce fotogramas
cronológicos al endpoint `responses` del propio `wrapper-backend`. Luna convierte
los fotogramas en evidencia visual y DeepSeek genera un workflow JSON con título,
resumen y pasos. El workflow queda dentro del mismo estado local aislado por
cuenta que el bot; eliminar el workflow o el bot elimina también su video.

`Run now` vuelve a ejecutar los pasos mediante el endpoint existente de Pi con
el navegador aislado y los conectores autorizados del bot. Esta función no
modifica `go_backend/pi_harness.py` ni comparte el perfil original grabado: si
la tarea necesita una sesión web, el usuario debe autorizarla en el perfil
temporal de esa ejecución o usar un conector OAuth. La extracción requiere un
plan con acceso a modelos y una sesión de Agent Genia iniciada.

### Login con Google

Electron usa exclusivamente `WRAPPER_SERVICE_URL` para el login Google, billing,
conectores y agentes. La build distribuida apunta a
`https://agentgenia-api.onrender.com`; desarrollo puede usar un loopback HTTP.

El flujo usa Authorization Code con PKCE, valida el `state` y consulta la
identidad verificada de Google. Google se identifica por su `sub` estable; una
instancia propia del wrapper nunca enlaza automáticamente por email con un
usuario del signup antiguo porque esos correos no fueron verificados.

El backend no persiste tokens de Google. Emite tokens opacos propios, guarda
solo sus hashes en la base de datos configurada, liga cada refresh token al
`device_id`, lo rota al usarlo y revoca el access token al cerrar sesión.
Electron cifra access, refresh e identidad con `safeStorage`/Keychain.

Para reemplazar el broker actual por una instancia propia:

1. Crea en Google Cloud un cliente OAuth 2.0 de tipo **Web application**.
2. Registra exactamente
   `https://api.tu-dominio.com/v1/account-auth/google/callback` como redirect URI.
3. Define `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` y
   `GOOGLE_OAUTH_REDIRECT_URI` únicamente en el entorno privado del backend.
4. Configura `HOST=0.0.0.0` y usa `DATABASE_URL` con Supabase/Postgres.
5. Define `WRAPPER_SERVICE_URL=https://api.tu-dominio.com` al compilar o lanzar
   Electron si no usas el dominio de producción predeterminado.

El catálogo de Agent Genia ofrece conexión real y aislada por usuario mediante
el gateway de Composio que vive dentro de `wrapper-backend`: Google
Workspace, Slack, Notion, LinkedIn, Zoom, GitHub, Jira, Linear, Asana, ClickUp,
Figma, Canva, Trello, monday.com, Intercom, Zendesk, Box, Dropbox, Calendly,
Stripe, QuickBooks, Greenhouse, Mailchimp, Shopify, Apollo, Ashby, Vercel, Hex,
Amplitude, Mixpanel y Databricks. Microsoft 365 y HubSpot usan también Managed
Auth. Los proveedores que requieren una app propia se activan con
`COMPOSIO_AUTH_CONFIGS_JSON`. La primera conexión abre el inicio de sesión de
Agent Genia y después el Connect Link oficial; los tokens administrados
permanecen en Composio, nunca en el renderer ni en Pi.

Cuando Composio no ofrece Managed Auth, `wrapper-backend` usa su adaptador REST
first-party para Nooks, Rippling, Salesloft, Tiendanube, Clay, DocuSign,
NetSuite, Outreach, Ramp, Tableau, WooCommerce, Workday y ZoomInfo. El usuario
abre un formulario de un solo uso y aporta la credencial de API que le entrega
su proveedor; el backend la cifra con `WRAPPER_SECRET` en
`connector_credentials`. Electron y Pi nunca reciben el secreto. Si hay un Auth
Config de Composio para el mismo proveedor, se prefiere su OAuth administrado.

Loom permanece explícitamente como `Próximamente`: está en el catálogo visual,
pero esta versión no registra un toolkit ni un adaptador first-party para sus
operaciones. Por eso el backend lo devuelve como no disponible y la interfaz no
permite presentarlo como una conexión real.

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
Databricks y Mailchimp. Un conector solo se habilita si tiene un toolkit OAuth
configurado o uno de los adaptadores first-party anteriores.

La sesión de Electron y el broker efímero de Pi son límites de confianza
distintos. Conectar una cuenta crea una conexión real en Composio; al ejecutar,
el adaptador registrado en `wrapper-backend` busca y llama la herramienta en
una sesión limitada al toolkit y al mismo usuario. Pi nunca recibe refresh
tokens, API keys de Composio ni client secrets. Electron consulta todas sus
conexiones con un único `GET /v1/connectors`; el backend es la fuente de verdad
y no persiste estados OAuth de proveedores en el dispositivo.

Cargar credenciales del proveedor al pool:

```bash
# key(s) a mano
.venv/bin/python -m go_backend.server add-key MODEL_KEY_1 MODEL_KEY_2

# desde stdin
echo "MODEL_KEY_1" | .venv/bin/python -m go_backend.server add-key -

# desde tu Keychain de macOS
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

El recorrido es `cliente → agent/run → Pi RPC → chat/completions → proveedor`.
Por eso las llamadas que hace Pi usan la capacidad asignada al
usuario y aparecen en `/v1/usage`. Cada ejecución tiene un workspace y logs
propios bajo `PI_RUNS_DIR`.

En producción Pi se ejecuta únicamente en Linux mediante el launcher
Bubblewrap fail-closed. Instala `bubblewrap`, `socat` y `util-linux`, ejecuta
`./scripts/setup-pi-sandbox.sh` y consulta el threat model y las pruebas
negativas en [docs/pi-sandbox.md](docs/pi-sandbox.md). No existe fallback al
binario de Pi sin aislamiento.

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
5. Composio conserva y refresca las credenciales; el adaptador se ejecuta dentro
   del backend. Si no existe o el usuario no inició sesión, la llamada falla cerrada
   con `connector_not_configured` o `connector_not_connected`.

La extensión y el broker no inventan una sesión OAuth: son la ruta segura entre
Pi y el gateway real. Los proveedores sin Managed Auth requieren un Auth Config
de Composio registrado por el operador. La selección visual de un bot solamente
determina el `connector_ids` que debe enviarse al ejecutar ese bot.

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

- Luna recibe las imágenes con la misma capacidad de modelos asignada al usuario.
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

## Computadora persistente por bot

La computadora de un bot es distinta del Chrome efímero anterior. Con
`COMPUTERS_ENABLED=1`, el backend crea una sandbox Daytona privada para la
combinación exacta `(usuario, bot)` y conserva su filesystem, perfil y sesiones
cuando se detiene. Electron permite crearla, abrir su viewer noVNC firmado e
hibernarla; Pi controla la misma máquina mediante la herramienta `computer`
(captura, mouse, teclado, shell y archivos), cargada por la extensión existente
de conectores. No se modifica `go_backend/pi_harness.py`.

Para habilitarla:

1. Crea una API key server-side en Daytona y define `DAYTONA_API_KEY`.
2. Define `COMPUTERS_ENABLED=1`. Opcionalmente usa `DAYTONA_SNAPSHOT` para una
   imagen preparada con Chromium y las aplicaciones que quieras entregar.
3. Aplica la migración `20260812233000_bot_computers.sql` en Supabase y despliega
   el backend. `PI_CONNECTOR_EXTENSION` debe seguir apuntando a la extensión
   first-party incluida en este repositorio.

El estado normal es `off → pulling → running → hibernated`. El auto-stop de 15
minutos conserva los datos y evita pagar cómputo ocioso; el auto-archive se
activa tras 24 horas detenida. Un viewer nunca se guarda en la base de datos:
se genera al abrir, expira y Electron solo acepta HTTPS (o HTTP loopback en
desarrollo). Eliminar un bot elimina primero su sandbox remota para no dejar
recursos facturables huérfanos.

## Endpoints

### Públicos o de intercambio de sesión

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/v1/account-auth/start` | Inicia Google OAuth para un `device_id` UUID |
| POST | `/v1/account-auth/status` | Consume una sola vez el intento enviado en el body y entrega la sesión al dispositivo original |
| GET | `/v1/account-auth/google/callback` | Callback exacto registrado en Google Cloud |
| GET | `/v1/account-auth/complete` | Página final del login |
| GET | `/connections/complete` | Página final de autorización de conectores |
| GET/POST | `/v1/connectors/native/setup/<attempt_id>` | Formulario de un solo uso para un adaptador first-party |
| POST | `/v1/signup` | Crea un usuario `free`; no acepta decisiones de tier ni asigna capacidad |
| POST | `/v1/billing/webhook` | Webhook que exige una firma `Stripe-Signature` válida |
| GET | `/healthz` | Liveness del proceso y estado resumido de dependencias |
| GET | `/readyz` | Readiness real; responde 503 si PostgreSQL/esquema no están listos |

Los formularios nativos usan un `attempt_id` aleatorio, de corta vida y ligado
al usuario que inició la conexión.

### Autenticados o ligados a una sesión

Las rutas de producto aceptan `Authorization: Bearer <api_key|access_token>`.
`refresh` recibe específicamente el refresh token como Bearer y el `device_id`
en el body. Los tokens de sesión pertenecen a un dispositivo; la API key
proviene del flujo de signup.

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/v1/account-auth/refresh` | Rota un refresh token enviado como Bearer y ligado al `device_id` |
| POST | `/v1/account-auth/logout` | Revoca la sesión actual |
| GET | `/v1/connectors` | Catálogo y conexiones del usuario |
| GET | `/v1/connectors/<id>` | Estado y disponibilidad de un conector |
| POST | `/v1/connectors/start` | Crea un Connect Link o formulario first-party |
| POST | `/v1/connectors/status` | Consulta y consume el consentimiento con `attempt_id` en el body |
| POST | `/v1/connectors/disconnect` | Revoca las cuentas del toolkit para el usuario |
| GET | `/v1/billing` | Estado de plan y suscripción del usuario autenticado |
| POST | `/v1/billing/checkout` | Abre Checkout con `{tier:"basic"|"pro"}`; el servidor fija el price ID |
| POST | `/v1/billing/portal` | Crea una sesión del Customer Portal para el customer ligado al usuario |
| POST | `/v1/byok` | El usuario registra su propia credencial de proveedor `{apiKey}` |
| GET | `/v1/models` | Catálogo de modelos del proveedor configurado |
| POST | `/v1/chat/completions` | Proxy OpenAI-compatible (stream y no-stream) |
| POST | `/v1/responses` | Proxy Responses API (stream y no-stream) |
| POST | `/v1/messages` | Proxy estilo Anthropic |
| GET | `/v1/usage` | Uso por ventanas con límites ajustados al tier |
| GET | `/v1/me` | Usuario, tier y suscripción asignada |
| GET | `/v1/agent/status` | Estado y capacidades habilitadas del harness de Pi |
| POST | `/v1/agent/run` | Ejecuta Pi con `{prompt, browser?: false, computer?: false, bot_id?: string, connector_ids?: string[]}` y espera el resultado |
| GET | `/v1/computers/<bot_id>` | Consulta estado sin despertar la computadora |
| POST | `/v1/computers/<bot_id>/ensure` | Crea/despierta y devuelve un viewer firmado de corta duración |
| POST | `/v1/computers/<bot_id>/hand-back` | Hiberna la computadora conservando datos y sesiones |
| POST | `/v1/computers/<bot_id>/delete` | Elimina la computadora remota antes de borrar el bot |

### Internos del runtime

Estas rutas solo aceptan loopback y un grant efímero emitido para una ejecución
de Pi. No son una API para clientes:

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/v1/internal/connectors/catalog` | Catálogo limitado a los conectores concedidos |
| POST | `/v1/internal/connectors/execute` | Ejecuta una operación autorizada del broker |
| POST | `/v1/internal/computers/execute` | Controla la computadora ligada al grant |

### Administración

Bearer = `ADMIN_TOKEN`:

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/admin/subscriptions` | Agregar credenciales de proveedor al pool `{keys:[...]}` |
| GET | `/admin/subscriptions` | Listar pool (keys cifradas, enmascaradas) |
| GET | `/admin/users` | Listar usuarios y asignaciones |
| POST | `/admin/users/<id>/revoke` | Deshabilitar la cuenta y revocar API key, sesiones, conectores y computadoras |
| POST | `/admin/users/<id>/tier` | Cambiar tier `{tier: "free"|"basic"|"pro"}` |
| GET | `/admin/usage` | Eventos de uso recientes |

## Uso y límites

- Los presupuestos internos son **$12 por 5 horas, $30 por semana, $60 por mes**.
- El tier del usuario escala esos límites (`basic` = 50%, `pro` = 100%).
- El backend estima el costo por request con la tabla de precios de
  `go_backend/go_prices.py` y responde `429 usage_limit` cuando el usuario
  alcanza un límite (configurable con `ENFORCE_LIMITS=0`).
- El uso se registra en `usage_events` con tokens input/output/cacheados y
  costo estimado. En streaming, el uso se captura del evento final si el
  upstream lo incluye (si no, se cuenta la request sin tokens).

## Configuración (env)

`.env.example` es la plantilla de despliegue. Las variables que reconoce el
runtime se agrupan aquí por función.

### Servidor, persistencia y cuenta

| Variable | Default | Descripción |
|---|---|---|
| `HOST` | `127.0.0.1` | Interfaz de escucha; usa `0.0.0.0` detrás de un proxy |
| `PORT` | `8787` | Puerto HTTP |
| `ENVIRONMENT` | `development` | Solo admite `development` o `production` |
| `WRAPPER_SECRET` | archivo local en desarrollo | Obligatoria en producción; clave maestra para cifrar credenciales |
| `WRAPPER_SECRET_VERSION` | `1` | Versión activa; los nuevos secretos se cifran con ella |
| `WRAPPER_SECRET_PREVIOUS_JSON` | `{}` | Versiones anteriores para rotación dual-read/new-write |
| `SECRET_FILE` | `data/secret.key` | Clave local cuando no se define `WRAPPER_SECRET` |
| `ADMIN_TOKEN` | efímero solo en desarrollo | Obligatorio en producción; nunca se imprime |
| `DATABASE_URL` | SQLite solo en desarrollo | PostgreSQL/Supabase y obligatorio en producción |
| `DB_PATH` | `data/wrapper.sqlite` | Base de datos SQLite |
| `GO_BASE_URL` | valor de `.env.example` | Upstream OpenAI-compatible; el nombre se conserva por compatibilidad histórica |
| `ENFORCE_LIMITS` | `1` | Rechazar al superar los presupuestos de uso |
| `GOOGLE_OAUTH_CLIENT_ID` | vacío | Cliente OAuth web de Google |
| `GOOGLE_OAUTH_CLIENT_SECRET` | vacío | Secreto OAuth, solo servidor |
| `GOOGLE_OAUTH_REDIRECT_URI` | vacío | Callback `/v1/account-auth/google/callback` registrado en Google |
| `ACCOUNT_ACCESS_TTL_SECONDS` | `900` | Vida del access token de Agent Genia |
| `ACCOUNT_REFRESH_TTL_SECONDS` | `2592000` | Vida del refresh token |
| `ACCOUNT_AUTH_ATTEMPT_TTL_SECONDS` | `600` | Vida del intento OAuth |
| `WRAPPER_SERVICE_URL` | producción | Backend utilizado por Electron |

### Stripe

| Variable | Default | Descripción |
|---|---|---|
| `STRIPE_ENABLED` | `0` | Habilita Checkout, Portal y webhook |
| `STRIPE_LIVE_MODE` | `1` | Exige recursos live; usa `0` únicamente en pruebas |
| `STRIPE_SECRET_KEY` | vacío | Secret key server-side |
| `STRIPE_WEBHOOK_SECRET` | vacío | Signing secret `whsec_...` |
| `STRIPE_PLUS_PRICE_ID` | vacío | Price allowlisted para `basic`/Plus |
| `STRIPE_PRO_PRICE_ID` | vacío | Price allowlisted para `pro` |
| `STRIPE_SUCCESS_URL` | vacío | Retorno exitoso de Checkout |
| `STRIPE_CANCEL_URL` | vacío | Retorno cancelado de Checkout |
| `STRIPE_PORTAL_RETURN_URL` | vacío | Retorno del Customer Portal |
| `STRIPE_WEBHOOK_TOLERANCE_SECONDS` | `300` | Tolerancia de firma del webhook |

### Conectores

| Variable | Default | Descripción |
|---|---|---|
| `COMPOSIO_API_KEY` | vacío | Credencial server-side del gateway |
| `COMPOSIO_PUBLIC_URL` | vacío | URL pública para callbacks de Composio |
| `CONNECTOR_PUBLIC_URL` | `COMPOSIO_PUBLIC_URL` | URL HTTPS para formularios first-party |
| `COMPOSIO_AUTH_CONFIGS_JSON` | `{}` | Mapa privado de connector ID a Auth Config |
| `COMPOSIO_TOOLKIT_OVERRIDES_JSON` | `{}` | Overrides de toolkit por connector ID |
| `COMPOSIO_AUTH_ATTEMPT_TTL_SECONDS` | `600` | Vida de un intento de conexión |

### Visión y Pi

| Variable | Default | Descripción |
|---|---|---|
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
| `PI_BIN` | `./scripts/pi-sandbox` | Launcher Bubblewrap fail-closed; ejecuta el Pi real dentro del sandbox Linux |
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

### Computadoras persistentes

| Variable | Default | Descripción |
|---|---|---|
| `COMPUTERS_ENABLED` | `0` | Habilita una sandbox Daytona persistente por `(usuario, bot)` |
| `DAYTONA_API_KEY` | vacío | Credencial server-side; obligatoria si la función está habilitada |
| `DAYTONA_API_URL` | vacío | Endpoint alternativo de Daytona |
| `DAYTONA_TARGET` | vacío | Target o región opcional |
| `DAYTONA_SNAPSHOT` | vacío | Snapshot opcional preparado con apps para la computadora |
| `COMPUTER_AUTO_STOP_MINUTES` | `15` | Inactividad antes de detener cómputo conservando el filesystem |
| `COMPUTER_AUTO_ARCHIVE_MINUTES` | `1440` | Tiempo detenida antes de archivar |
| `COMPUTER_PREVIEW_TTL_SECONDS` | `3600` | Vigencia del viewer firmado solicitado por Electron |
| `COMPUTER_VNC_PORT` | `6080` | Puerto noVNC expuesto mediante preview firmado |
| `COMPUTER_VNC_RESOLUTION` | `1440x900` | Resolución fija del escritorio al crear la sandbox |
| `COMPUTER_BASIC_LIMIT` | `1` | Máximo de computadoras persistentes para un usuario Plus |
| `COMPUTER_PRO_LIMIT` | `3` | Máximo de computadoras persistentes para un usuario Pro |

## Seguridad

- Las credenciales del proveedor se cifran con AES-256-GCM (`cryptography`) o se guardan en
  el Keychain de macOS (fallback). Nunca se persisten en claro.
- Las api keys de los usuarios del wrapper se guardan hasheadas (SHA-256);
  solo se muestran una vez en el signup.
- El signup público siempre crea `free`. Solo una transición autenticada tras
  comprobar el pago puede activar `basic`/`pro` y reclamar capacidad.
- Los eventos Stripe se verifican con HMAC, tolerancia temporal y `livemode`;
  `stripe_events.event_id` hace su procesamiento idempotente y
  `event.created` impide que un webhook antiguo sobrescriba el estado más
  reciente de una suscripción. Antes de conceder un entitlement pagado, el
  backend recupera la suscripción actual desde Stripe. Customer, suscripción,
  usuario, tier y capacidad se enlazan en una sola transacción.
- `STRIPE_SECRET_KEY` y `STRIPE_WEBHOOK_SECRET` solo viven en el backend. El
  arranque rechaza claves test en modo live, URLs inseguras y price IDs inválidos.
- En SQLite, la asignación usa `BEGIN IMMEDIATE`; en PostgreSQL, la misma
  sección crítica usa una transacción con advisory lock. En ambos motores,
  `uniq_user_subscription` y la actualización atómica impiden que dos
  activaciones conserven la misma key.
- El servidor se niega a arrancar con `ADMIN_TOKEN=cambia-este-token`. En
  `ENVIRONMENT=production` también exige `DATABASE_URL`, `WRAPPER_SECRET` y
  `ADMIN_TOKEN`; no genera ni imprime ningún token.
- `data/secret.key` (0600) existe únicamente para desarrollo. Producción usa
  una clave de entorno versionada: los secretos nuevos usan
  `WRAPPER_SECRET_VERSION` y las versiones anteriores permanecen temporalmente
  en `WRAPPER_SECRET_PREVIOUS_JSON` para descifrado y rotación perezosa.
- OAuth y consentimientos se persisten con identificadores hasheados. Los
  resultados se consultan por POST y se consumen atómicamente una sola vez.
- Toda respuesta JSON usa `Cache-Control: no-store`; los errores 500 exponen
  solo `Internal server error` y un `request_id`, mientras la excepción completa
  permanece en los logs privados.
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
- Las computadoras persistentes sí conservan cookies, pero solo dentro de la
  sandbox privada de ese usuario y bot. El API key del proveedor nunca llega a
  Electron o Pi; cada ejecución recibe un grant revocable para un único bot y
  los viewers son URLs firmadas que no se persisten.

## Distribución, despliegue y documentos del producto

El repositorio incluye CI desde clean checkout y pipelines de publicación
fail-closed para las tres aplicaciones:

- `.github/workflows/release-config.yml`: comprueba versiones coordinadas,
  archivos obligatorios, acciones fijadas por SHA y YAML válido;
- `.github/workflows/desktop-release.yml`: firma/notariza Electron, genera
  instaladores, checksums, metadata de update y GitHub Release;
- `.github/workflows/ios-release.yml`: prueba, archiva, exporta y carga la IPA a
  App Store Connect/TestFlight;
- `.github/workflows/android-release.yml`: prueba, firma, verifica y publica el
  AAB en Google Play;
- `Dockerfile` + `render.yaml`: despliegue reproducible del backend, con secretos
  inyectados por Render.

Las credenciales y el procedimiento exacto están en
[docs/distribution.md](docs/distribution.md). El código es propietario bajo
[LICENSE](LICENSE). La operación pública debe enlazar
[Privacidad](PRIVACY.md), [Términos](TERMS.md), [EULA](EULA.md) y
[Seguridad](SECURITY.md) desde el website y las fichas de tienda.

## Tests

```bash
# Backend, billing, persistencia, visión, conectores y computadoras
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# Electron y extensión dinámica de conectores
pnpm test:desktop
pnpm test:connectors

# iOS: compila unit/UI tests sin firma y después ejecútalos en un simulador
xcodebuild \
  -project ios/AgentGenia/AgentGenia.xcodeproj \
  -scheme AgentGenia \
  -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO build-for-testing

xcodebuild \
  -project ios/AgentGenia/AgentGenia.xcodeproj \
  -scheme AgentGenia \
  -destination 'platform=iOS Simulator,name=iPhone 17' test

# Android (requiere JDK 17 y Android SDK Platform 36 estable)
cd android/AgentGenia
./gradlew testDebugUnitTest lintDebug assembleDebug connectedDebugAndroidTest
```

Las pruebas automáticas usan upstreams y servicios falsos: no consumen saldo del
proveedor ni realizan cobros. Cubren signup siempre-free, activación, carrera de
asignación, proxy y streaming, uso, tiers, BYOK, Stripe, cifrado, Google OAuth,
Pi RPC, visión, grants efímeros, conectores, computadoras, aislamiento por
cuenta, widgets del LLM y contratos de Electron. Las compilaciones móviles
validan además que los proyectos nativos y sus catálogos de assets sean válidos.
