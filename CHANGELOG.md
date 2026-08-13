# Changelog

Este proyecto sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y versionado semántico para los clientes distribuidos.

## [Unreleased]

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
