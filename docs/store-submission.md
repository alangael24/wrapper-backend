# Declaraciones de tienda — checklist

Este archivo es la fuente operativa; las respuestas finales deben reflejar la
configuración exacta de producción el día del envío.

## Datos compartidos por plataforma

| Categoría | Ejemplo | Ligado al usuario | Tracking | Finalidad |
|---|---|---:|---:|---|
| Nombre/email/user ID | Google OAuth / Sign in with Apple | Sí | No | Cuenta y seguridad |
| Contenido de usuario | prompts, archivos, capturas, respuestas | Sí | No | Funcionalidad del agente |
| Compras | plan y estado; no tarjeta | Sí | No | Entitlement/soporte |
| Device ID | UUID aleatorio de la instalación; rota al eliminar la cuenta | Sí | No | Rotación y revocación de sesión |
| Ubicación aproximada | IP observada por el backend | Sí | No | Seguridad y prevención de abuso |
| Diagnóstico | request ID, error, timestamps | Puede ser | No | Seguridad y confiabilidad |
| Uso | tokens, costo, tarea ejecutada | Sí | No | Límites y operación |

No hay SDK publicitario, ATT, venta de datos ni tracking cross-app. iOS incluye
`PrivacyInfo.xcprivacy`; Android desactiva backup/transferencia y cifra sesión y
estado mediante Android Keystore.

## App Store Connect

- Bundle ID: `com.agentgenia.ios`.
- Privacy Policy URL y Terms URL: publicar los documentos raíz en HTTPS.
- Account deletion URL: `https://agentgenia-api.onrender.com/account-deletion`.
- Sign in: Google y Sign in with Apple; entregar cuenta de revisión o
  instrucciones verificables.
- Compras en Release: no se muestran enlaces ni Checkout externo.
- Encryption/export compliance: `ITSAppUsesNonExemptEncryption=NO`; la app solo
  usa HTTPS, Keychain y APIs criptográficas estándar del sistema.
- Adjuntar notas que expliquen bots, conectores OAuth y viewer de computadora.

## Google Play Console

- Package: `com.agentgenia.android`.
- Target SDK: Android 16 / API 36 estable.
- Data Safety: declarar las categorías de la tabla, cifrado en tránsito y flujo
  de eliminación; no marcar “no se recopilan datos”.
- Account deletion URL: `https://agentgenia-api.onrender.com/account-deletion`;
  el mismo flujo también está dentro de la pantalla Cuenta.
- App Access: entregar instrucciones/cuenta para Google login y plan con acceso.
- Financial features: la app muestra estado del plan, no procesa tarjeta.
- Ads: No.
- Target audience: no dirigida a niños.
- Release billing: no se muestran enlaces ni Stripe externo.
- Probar el AAB del track internal antes de promoverlo.

## Evidencia que se conserva

Cada release debe guardar logs de CI, resultado de tests, archive/AAB/instaladores,
checksums, provenance y commit/tag. Certificados, profiles, service accounts y
API keys nunca se guardan como artefactos.
