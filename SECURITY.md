# Política de seguridad de Agent Genia

## Versiones soportadas

Solo la versión estable más reciente publicada en GitHub Releases, App Store o
Google Play recibe correcciones de seguridad. Una release retirada deja de
estar soportada en cuanto existe una sustitución segura.

## Reportar una vulnerabilidad

No abras un issue público. Envía el reporte a `security@agentgenia.com` con:

- producto, versión y plataforma afectada;
- pasos mínimos de reproducción;
- impacto observado o posible;
- logs redactados, sin tokens, cookies, llaves ni datos de terceros;
- una forma segura de contactarte.

Confirmaremos recepción en un máximo objetivo de 3 días hábiles y comunicaremos
triage y corrección según severidad. Solicitamos 90 días de divulgación
coordinada, salvo que acordemos otro plazo. No prometemos recompensas económicas.

## Alcance prioritario

Son especialmente relevantes: evasiones del sandbox de Pi, acceso cruzado entre
cuentas o bots, exposición de secretos, OAuth account takeover, ejecución no
autorizada de conectores, bypass de Stripe entitlements, VNC/computadoras de
otros usuarios y fallos de firma o actualización.

## Manejo de secretos

Los secretos de producción se guardan en Supabase/host y GitHub Actions
Environments. Nunca deben aparecer en issues, artefactos, screenshots ni logs.
Si sospechas exposición, revoca primero el secreto afectado y luego reporta el
incidente. Consulta [docs/distribution.md](docs/distribution.md) para los nombres
de secretos de firma y publicación.
