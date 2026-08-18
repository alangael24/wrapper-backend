import { access, readFile } from "node:fs/promises";

const requiredFiles = [
  "LICENSE", "SECURITY.md", "PRIVACY.md", "TERMS.md", "EULA.md", "CHANGELOG.md",
  "Dockerfile", ".dockerignore", "render.yaml", "electron-builder.yml",
  "requirements.in", "requirements.txt",
  ".github/workflows/backend.yml", ".github/workflows/release-config.yml",
  ".github/workflows/desktop.yml",
  ".github/workflows/desktop-release.yml", ".github/workflows/desktop-rollback.yml",
  ".github/workflows/ios.yml",
  ".github/workflows/ios-release.yml", ".github/workflows/android.yml",
  ".github/workflows/android-release.yml",
  "docs/distribution.md", "docs/store-submission.md",
  "scripts/verify-live-readiness.py",
  "ios/AgentGenia/AgentGenia.xcodeproj/xcshareddata/xcschemes/AgentGenia.xcscheme",
  "ios/AgentGenia/AgentGenia/PrivacyInfo.xcprivacy",
  "ios/AgentGenia/AgentGenia/AgentGenia.entitlements",
  "ios/AgentGenia/AgentGeniaTests/ModelsTests.swift",
  "ios/AgentGenia/AgentGeniaUITests/AgentGeniaUITests.swift",
  "android/AgentGenia/app/src/androidTest/java/com/agentgenia/android/SecureStoreInstrumentedTest.kt",
  "android/AgentGenia/app/src/androidTest/java/com/agentgenia/android/AppLaunchInstrumentedTest.kt",
  "desktop/resources/appx/StoreLogo.png",
  "desktop/resources/appx/Square150x150Logo.png",
  "desktop/resources/appx/Square44x44Logo.png",
  "desktop/resources/appx/Wide310x150Logo.png",
  "desktop/resources/eula.txt",
];
await Promise.all(requiredFiles.map((file) => access(file)));

const packageJson = JSON.parse(await readFile("package.json", "utf8"));
const project = await readFile("ios/AgentGenia/AgentGenia.xcodeproj/project.pbxproj", "utf8");
const gradle = await readFile("android/AgentGenia/app/build.gradle.kts", "utf8");
const builder = await readFile("electron-builder.yml", "utf8");
const render = await readFile("render.yaml", "utf8");
const backendWorkflow = await readFile(".github/workflows/backend.yml", "utf8");
const desktopReleaseWorkflow = await readFile(".github/workflows/desktop-release.yml", "utf8");
const androidReleaseWorkflow = await readFile(".github/workflows/android-release.yml", "utf8");
const dockerfile = await readFile("Dockerfile", "utf8");
const pythonLock = await readFile("requirements.txt", "utf8");
const entitlements = await readFile("ios/AgentGenia/AgentGenia/AgentGenia.entitlements", "utf8");
const workflowFiles = requiredFiles.filter((file) => file.startsWith(".github/workflows/"));

const iosVersion = project.match(/MARKETING_VERSION = ([^;]+);/)?.[1];
const androidVersion = gradle.match(/versionName = "([^"]+)"/)?.[1];
if (!iosVersion || !androidVersion) throw new Error("Unable to read mobile versions");
if (packageJson.version !== iosVersion || packageJson.version !== androidVersion) {
  throw new Error(`Version mismatch: desktop=${packageJson.version}, ios=${iosVersion}, android=${androidVersion}`);
}
if (!/^\d+\.\d+\.\d+$/.test(packageJson.devDependencies?.electron ?? "")) {
  throw new Error("Electron must be pinned to an exact stable version");
}
if (!project.includes("AgentGeniaTests") || !project.includes("AgentGeniaUITests")) {
  throw new Error("iOS test targets are missing");
}
if (!project.includes("INFOPLIST_KEY_ITSAppUsesNonExemptEncryption = NO")) {
  throw new Error("iOS export-compliance declaration is missing");
}
if (!project.includes("CODE_SIGN_ENTITLEMENTS = AgentGenia/AgentGenia.entitlements")) {
  throw new Error("iOS release is missing its entitlements file");
}
if (!entitlements.includes("com.apple.developer.applesignin")) {
  throw new Error("Sign in with Apple capability is missing");
}
if (!gradle.includes("signingConfig = signingConfigs.getByName(\"release\")")) {
  throw new Error("Android release signing is not fail-closed");
}
if (!gradle.includes("compileSdk = 36") || !gradle.includes("targetSdk = 36")) {
  throw new Error("Android releases must use the stable API 36 SDK");
}
for (const artifact of ["lifecycle-runtime-compose", "lifecycle-viewmodel-compose"]) {
  if (!gradle.includes(`${artifact}:2.10.0`)) {
    throw new Error(`${artifact} must remain on the API 36-compatible release`);
  }
}
for (const token of ["target: dmg", "target: pkg", "target: nsis", "target: appx", "AppImage", "deb", "rpm"]) {
  if (!builder.includes(token)) throw new Error(`Missing Electron target: ${token}`);
}
if (!builder.includes("x64ArchFiles: Contents/Resources/pi-runtime/pi-computer-use/prebuilt/macos/**/bridge")) {
  throw new Error("Universal macOS releases must preserve the architecture-specific computer-use helpers");
}
if (!/linux:\s+[\s\S]*?executableName: agent-genia/u.test(builder)) {
  throw new Error("Linux executable name must match the packaged smoke test");
}
for (const [key, value] of [
  ["PUBLIC_LEGACY_SIGNUP_ENABLED", "0"],
  ["STRIPE_ENABLED", "1"],
  ["CREDITS_MODE", "enforce"],
  ["COMPUTERS_ENABLED", "0"],
  ["EXTERNAL_WRITES_ENABLED", "0"],
  ["PI_ENABLED", "1"],
  ["PI_MAX_CONCURRENT", "4"],
  ["PI_BROWSER_MAX_CONCURRENT", "0"],
  ["PI_CHROME_AUTO_AUTHORIZE", "0"],
  ["DESKTOP_RUNTIME_PUBLIC_URL", "https://agentgenia-api.onrender.com"],
]) {
  const pattern = new RegExp(`key: ${key}\\s+value: ["']?${value}["']?`, "u");
  if (!pattern.test(render)) throw new Error(`render.yaml must set ${key}=${value}`);
}
if (!/key: PI_CHROME_ISOLATION\s+value: per_run/u.test(render)) {
  throw new Error("The legacy server-side pi-chrome guard must remain per_run");
}
if (!render.includes("autoDeployTrigger: checksPass")) {
  throw new Error("Render must wait for GitHub CI before deploying");
}
if (!render.includes("healthCheckPath: /readyz")) {
  throw new Error("Render must gate traffic on full production readiness");
}
if (!backendWorkflow.includes("pip-audit --require-hashes --disable-pip -r requirements.txt")) {
  throw new Error("Backend CI must audit the complete hashed Python lockfile");
}
if (!backendWorkflow.includes("pnpm audit --prod")) {
  throw new Error("Backend CI must audit production Node dependencies");
}
if (!dockerfile.includes("pip install --no-cache-dir --require-hashes -r requirements.txt")) {
  throw new Error("The production image must enforce hashes from requirements.txt");
}
if (!pythonLock.includes("--hash=sha256:")) {
  throw new Error("requirements.txt must be a hashed dependency lockfile");
}
if (!desktopReleaseWorkflow.includes("inputs.platforms == 'windows'")
  || !desktopReleaseWorkflow.includes("mac_pkg")
  || !desktopReleaseWorkflow.includes("electron-builder --mac")
  || !desktopReleaseWorkflow.includes("--universal --publish never")
  || !desktopReleaseWorkflow.includes("electron-builder --win --publish never -c.forceCodeSigning=true")
  || !desktopReleaseWorkflow.includes("electron-builder --linux --publish never -c.forceCodeSigning=false")
  || !desktopReleaseWorkflow.includes("already exists and is immutable")
  || !desktopReleaseWorkflow.includes("gh release create")
  || desktopReleaseWorkflow.includes("gh release delete-asset")
  || desktopReleaseWorkflow.includes("--clobber")
  || !desktopReleaseWorkflow.includes("attestations: write")) {
  throw new Error("Desktop release must append Windows only through its signed, checksum-preserving path");
}
if (!androidReleaseWorkflow.includes("if: inputs.publish_to_play == true")
  || !androidReleaseWorkflow.includes("if: inputs.publish_to_play != true")
  || !androidReleaseWorkflow.includes("refusing to replace its immutable AAB")
  || !androidReleaseWorkflow.includes("attest_existing_release")
  || !androidReleaseWorkflow.includes("attestations: write")) {
  throw new Error("Android release must separate immutable signed AAB publication from Google Play promotion");
}
for (const key of [
  "DATABASE_URL", "WRAPPER_SECRET", "ADMIN_TOKEN", "DEEPSEEK_API_KEY",
  "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI",
  "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN",
  "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_PUBLIC_NUMBER",
  "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY_BASE64",
  "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_STARTER_PRICE_ID",
  "STRIPE_PRO_PRICE_ID", "STRIPE_BUSINESS_PRICE_ID", "COMPOSIO_API_KEY",
  "COMPOSIO_AUTH_CONFIGS_JSON", "DAYTONA_API_KEY",
]) {
  if (!new RegExp(`key: ${key}\\s+sync: false`, "u").test(render)) {
    throw new Error(`render.yaml must require secret ${key}`);
  }
}

const expectedMigrations = [
  ["20260812020006_agentgenia_private_schema.sql", 4],
  ["20260812092839_connector_credentials.sql", 5],
  ["20260812174201_bot_computers.sql", 6],
  ["20260812174212_stripe_event_ordering.sql", 7],
  ["20260812180737_production_security_hardening.sql", 8],
  ["20260813032540_apple_identity_tokens.sql", 9],
  ["20260813143000_deepseek_direct.sql", 10],
  ["20260813190000_credit_ledger.sql", 11],
  ["20260813200000_account_model_provider_override.sql", 12],
  ["20260813224341_account_state_sync.sql", 13],
  ["20260814045554_production_audit_hardening.sql", 14],
  ["20260814052000_distributed_rate_limits.sql", 15],
  ["20260814070000_run_recovery_and_retention.sql", 16],
  ["20260814120000_whatsapp_channel.sql", 17],
  ["20260814123000_whatsapp_channel_rls.sql", 17],
  ["20260817120000_connector_operation_idempotency.sql", 18],
  ["20260817123000_account_bot_tombstones.sql", 19],
  ["20260818013000_structured_action_approvals.sql", 20],
  ["20260818173000_desktop_runtime_relay.sql", 21],
  ["20260818214612_whatsapp_outbound_status.sql", 22],
];
const migrationContents = await Promise.all(
  expectedMigrations.map(([name]) => readFile(`supabase/migrations/${name}`, "utf8")),
);
for (const [index, contents] of migrationContents.entries()) {
  const [name, expectedVersion] = expectedMigrations[index];
  if (!contents.includes(`values ('schema_version', '${expectedVersion}')`)) {
    throw new Error(`${name} must set schema_version=${expectedVersion}`);
  }
}
const whatsappOutboundMigration = migrationContents.at(-1);
if (!whatsappOutboundMigration.includes("'sending'")
  || !whatsappOutboundMigration.includes("whatsapp_messages_status_check")) {
  throw new Error("WhatsApp delivery migration must permit the durable sending state");
}
for (const file of workflowFiles) {
  const workflow = await readFile(file, "utf8");
  if (/uses:\s+[^\s]+@v\d+/u.test(workflow)) {
    throw new Error(`${file} contains a mutable action version instead of a commit SHA`);
  }
}
for (const file of [
  ".github/workflows/desktop-release.yml",
  ".github/workflows/ios-release.yml",
  ".github/workflows/android-release.yml",
]) {
  const workflow = await readFile(file, "utf8");
  if (!workflow.includes("python scripts/verify-live-readiness.py")) {
    throw new Error(`${file} must block distribution when production is not ready`);
  }
  if (!workflow.includes("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065")) {
    throw new Error(`${file} must use the pinned release Python runtime`);
  }
  if (!workflow.includes("git merge-base --is-ancestor HEAD origin/main")) {
    throw new Error(`${file} must refuse tags that are not reachable from main`);
  }
}
for (const file of [".github/workflows/desktop.yml", ".github/workflows/desktop-release.yml"]) {
  const workflow = await readFile(file, "utf8");
  if (!workflow.includes("sudo chmod 4755 \"$sandbox\"")) {
    throw new Error(`${file} must launch Linux Electron with its sandbox configured`);
  }
  if (workflow.includes("--no-sandbox")) {
    throw new Error(`${file} must not disable the Electron sandbox`);
  }
}
console.log(`Release configuration verified for Agent Genia ${packageJson.version}.`);
