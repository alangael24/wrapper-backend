# Changelog

Este proyecto sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y versionado semántico para los clientes distribuidos.

## [Unreleased]

## [1.1.2] - 2026-08-18

- Reduce desktop end-to-end agent latency by long-polling relay jobs, reusing the local model runtime and resource loader, and removing redundant backend state reads and writes.
- Align Android agent prewarming with desktop and iOS.
- Gate desktop releases on the production local-computer relay instead of the retired server-side Pi Chrome runtime.

## [1.1.1] - 2026-08-14

### Fixed

- Desktop replaces the legacy 1.1.0 client that could block logout while
  disconnecting obsolete providers and never synchronized bots with the
  account-scoped backend state.
- Existing plaintext Desktop bots migrate once into the encrypted per-account
  cache and then synchronize across Desktop, iOS and Android.

### Added

- Wallet de créditos entero y auditable con grants, reservas atómicas por run,
  allocations, liquidación por consumo real y tokens efímeros exclusivos de Pi.
- Planes Starter, Pro y Business con grants mensuales idempotentes por periodo
  de Stripe, concurrencia por usuario y trial único de 30 créditos.
- Endpoints de balance y ajustes administrativos, costo en microUSD por llamada
  y límites autorizados por ejecución con modo inicial `shadow`.

### Changed

- Desktop, iOS y Android envían una clave de idempotencia y un máximo de
  créditos por trabajo; la facturación muestra el catálogo actual del backend.
- El esquema de persistencia sube a la versión 13.

### Fixed

- La ruta caliente de agentes ilimitados usa una sola reserva/liquidación SQL,
  reutiliza la autenticación de cuenta y evita revalidar en PostgreSQL el token
  efímero que el mismo proceso acaba de emitir.
- iOS abre inmediatamente desde su estado local, reconcilia cambios entre
  dispositivos en segundo plano y no bloquea el chat esperando esa escritura.
- El streaming conserva widgets generados por el modelo, agrupa deltas para
  reducir trabajo de UI y reintenta el precalentamiento idempotente tras un
  corte transitorio de red.
- Cada turno reutiliza el proceso Pi caliente con un contexto interno efímero y
  acotado; iOS evita la carrera entre warm-up/primer mensaje y solicita tiempo
  de ejecución al pasar brevemente a segundo plano.

## [1.0.1] - 2026-08-12

### Added

- Sign in with Apple nativo y revocación verificable al eliminar la cuenta.
- Eliminación completa de cuenta desde Electron, iOS y Android.
- Gate de producción que bloquea releases cuando `/readyz` no está operativo.

### Fixed

- `cryptography` actualizado a 50.0.0 para corregir `PYSEC-2026-3552` antes de
  construir imágenes o aplicaciones distribuibles.
- Health checks separados para liveness, plataforma y dependencias completas.
- Estado, grabaciones y device IDs locales se eliminan con la cuenta.
- Los auto-deploys de Render ahora esperan a que CI termine correctamente.
- Cuerpos JSON acotados, readiness sin metadatos internos y rate limit para Apple.

## [1.0.0] - 2026-08-12

### Added

- Pipeline firmado de Electron para macOS, Windows y Linux con checksums,
  actualización automática y retiro de emergencia.
- Archive/TestFlight automatizado y targets de unit/UI tests para iOS.
- AAB firmado, pruebas instrumentadas y publicación automatizada a Google Play.
- Contenedor y manifiesto de Render reproducibles.
- Licencia propietaria, EULA, términos, privacidad y política de seguridad.
- Aplicaciones Agent Genia para Electron, iOS y Android.
- Agentes con widgets generados por el modelo, conectores OAuth y computadoras
  persistentes aisladas por bot.
- Billing verificado por Stripe y backend de producción sobre PostgreSQL.
