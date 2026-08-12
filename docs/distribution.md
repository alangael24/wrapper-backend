# Distribución de Agent Genia

Las builds de release son fail-closed: si falta una firma, perfil o credencial de
tienda, el workflow termina sin publicar. Ningún secreto vive en Git.
Configura el environment protegido `production-release` en GitHub, limita sus
deployment branches a tags protegidos y exige aprobación manual antes de exponer
los secretos de firma a cada job.

## Versionado y tags

Actualiza de forma coordinada `package.json`, `MARKETING_VERSION` de iOS y
`versionName` de Android. Usa tags separados para evitar publicar plataformas por
accidente:

```text
desktop-v1.0.0   → GitHub Release + canal electron-updater
ios-v1.0.0       → App Store Connect / TestFlight
android-v1.0.0   → Google Play + GitHub Release del AAB
```

Los build numbers móviles se derivan determinísticamente de SemVer y una revisión
de reintento entre 0 y 99: `(major*1,000,000 + minor*1,000 + patch)*100 +
revision`. El tag automático usa revisión 0. Si la tienda aceptó el binario pero
el job perdió la respuesta, relanza manualmente el mismo tag con una revisión
superior; no reescribas el tag. Un tag debe apuntar al commit exacto que pasó CI.
Las Actions también admiten `workflow_dispatch` pero exigen que el tag ya exista.

## Electron

`electron-builder.yml` produce:

- macOS universal: DMG, PKG y ZIP, con Developer ID, hardened runtime y
  notarización;
- Windows x64: NSIS EXE y AppX/MSIX-compatible, con Authenticode;
- Linux x64: AppImage, deb y rpm;
- metadatos de actualización, blockmaps y `SHA256SUMS.txt`.

La build usa Electron 43.4.0 fijado en el lockfile. Los artefactos objetivo son
macOS 12 o posterior (Intel y Apple Silicon), Windows 10/11 x64 y Linux x64.

El `.app` dentro de DMG/ZIP se firma con Developer ID Application. El PKG se
firma además con Developer ID Installer. El workflow somete los contenedores DMG
y PKG finales a `notarytool`, grapa el ticket y los valida; no aplica una firma
separada al contenedor DMG porque Gatekeeper valida la app firmada y
electron-builder desaconseja combinar esa firma redundante con notarización.

Secrets de GitHub Actions:

| Secret | Uso |
|---|---|
| `MAC_CSC_LINK` | P12 de Developer ID Application en base64 o enlace privado |
| `MAC_CSC_KEY_PASSWORD` | Contraseña del P12 |
| `MAC_INSTALLER_CSC_LINK` | P12 separado de Developer ID Installer para el PKG |
| `MAC_INSTALLER_CSC_KEY_PASSWORD` | Contraseña del certificado Installer |
| `APPLE_API_KEY_BASE64` | API key `.p8` codificada en base64 |
| `APPLE_API_KEY_ID` / `APPLE_API_ISSUER` | Notarización/App Store Connect |
| `WINDOWS_CSC_LINK` | PFX de firma Windows en base64 o enlace privado |
| `WINDOWS_CSC_KEY_PASSWORD` | Contraseña del PFX |
| `WINDOWS_APPX_PUBLISHER` | Subject exacto del certificado, por ejemplo `CN=...` |

La app revisa GitHub Releases 15 segundos después de arrancar y cada 4 horas.
Descarga en segundo plano e instala al salir. No admite downgrade silencioso.
Para detener una release defectuosa ejecuta **Desktop Update Rollback**, escribe
`WITHDRAW` y selecciona el tag: la release pasa a draft y deja de ofrecerse a
nuevos clientes. Los ya actualizados requieren publicar la última versión buena
como un patch SemVer superior; nunca se reescribe un tag ni un artefacto.

## iOS

El scheme compartido ejecuta `AgentGeniaTests` y `AgentGeniaUITests`. `iOS CI`
compila los test bundles, ejecuta tests en un simulador limpio y valida un
archive Release sin firma. `iOS TestFlight Release` importa temporalmente:

- `IOS_DISTRIBUTION_CERTIFICATE_BASE64` y
  `IOS_DISTRIBUTION_CERTIFICATE_PASSWORD`;
- `IOS_PROVISIONING_PROFILE_BASE64` y `IOS_PROVISIONING_PROFILE_NAME`;
- `APPLE_TEAM_ID`, `APPLE_API_KEY_BASE64`, `APPLE_API_KEY_ID` y
  `APPLE_API_ISSUER`.

La app `com.agentgenia.ios` debe existir en App Store Connect y el profile debe
coincidir exactamente. El workflow prueba, archiva, exporta, valida y sube la IPA;
Apple todavía debe procesarla y el operador debe asignarla a TestFlight o enviar
la versión a revisión.

La build Release no muestra Checkout/Portal externos. Los usuarios pueden usar
un plan comprado fuera de la app, pero no existe CTA de compra dentro de iOS.
Si se quiere vender dentro de la app, primero hay que implementar StoreKit y
mapear recibos a entitlements del backend.

## Android

`Android` ejecuta unit tests, lint, APK Debug y pruebas instrumentadas de inicio,
Compose y Android Keystore. `Android Play Release` exige:

- `ANDROID_RELEASE_KEYSTORE_BASE64`;
- `ANDROID_RELEASE_STORE_PASSWORD`;
- `ANDROID_RELEASE_KEY_ALIAS`;
- `ANDROID_RELEASE_KEY_PASSWORD`;
- `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`.

El servicio de Google debe tener permiso de release sobre
`com.agentgenia.android`. El workflow verifica `jarsigner`, publica primero al
track solicitado (`internal` por defecto), conserva el AAB/checksum en GitHub y
emite provenance. La build distribuible usa Android 16/API 36 estable (no el SDK
preview de Android 17), que cumple el requisito de Google Play vigente desde el
31 de agosto de 2026. La build Release oculta Stripe; para venta móvil futura se
debe integrar Google Play Billing y validación server-side.

## Store metadata obligatoria

Antes de revisión, publica `PRIVACY.md` y `TERMS.md` en URLs HTTPS estables de
`agentgenia.com`, añade un enlace a eliminación de cuenta y completa:

- App Store Privacy, screenshots, edades, soporte, política y notas de revisión;
- Google Play Data Safety, App Access, content rating, ads declaration, account
  deletion y ficha de tienda;
- clasificación/identidad de Windows Store si se publica AppX por Partner Center.

La declaración inicial está en [store-submission.md](store-submission.md), pero
debe compararse con los proveedores y datos realmente habilitados al enviar.

## Backend reproducible

`Dockerfile` fija Python, Node y pnpm, instala Bubblewrap/Chromium y usa un usuario
sin privilegios. `render.yaml` exige que producción reciba `DATABASE_URL`,
`WRAPPER_SECRET`, `ADMIN_TOKEN` y los secretos de proveedores desde Render. Las
funciones costosas (`PI_ENABLED`, `COMPUTERS_ENABLED`, `STRIPE_ENABLED`) empiezan
apagadas y se activan solo después de configurar sus secretos y verificar
`/readyz`.

El sandbox Bubblewrap requiere un host Linux con user namespaces habilitados. Si
el runtime de contenedores los bloquea, `/v1/agent/status` debe permanecer
deshabilitado; nunca cambies `PI_BIN` al ejecutable sin sandbox.
