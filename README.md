# Wrapper Backend — OpenCode Go por usuario

Backend para tu wrapper: **cada usuario nuevo recibe automáticamente una
suscripción de OpenCode Go asignada** (una key por usuario). El backend
proxya las requests de LLM al upstream de Go con la key de ese usuario,
registra uso y vigila los límites de la suscripción.

Cero dependencias fuera del stdlib excepto `cryptography` (para cifrar las
keys en reposo). Python 3.12.

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
- `cryptography` (solo para cifrado AES; sin él usa el Keychain de macOS):

```bash
python3.12 -m venv .venv
.venv/bin/pip install cryptography
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

## Seguridad

- Las keys de Go se cifran con AES-256-GCM (`cryptography`) o se guardan en
  el Keychain de macOS (fallback). Nunca se persisten en claro.
- Las api keys de los usuarios del wrapper se guardan hasheadas (SHA-256);
  solo se muestran una vez en el signup.
- Los secretos viven en `data/secret.key` (0600); si usas `WRAPPER_SECRET`
  en producción, bórralo y apóyalo en tu gestor de secretos.

## Tests

```bash
.venv/bin/python tests/test_backend.py
```

Corren contra un upstream mock (sin llamadas reales a OpenCode Go) y cubren:
signup/asignación, proxy de modelos, chat/responses/messages, streaming,
registro de uso, límite 429, tiers (free/basic/pro), BYOK, revocación y
cifrado en reposo.
