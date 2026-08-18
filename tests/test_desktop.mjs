import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const contracts = require("../desktop/dist/contracts.cjs");

test("ships the complete connector catalog from work and commerce apps", () => {
  const ids = contracts.CONNECTOR_CATALOG.map((item) => item.id);
  assert.equal(ids.length, 49);
  for (const id of [
    "google-workspace", "slack", "notion", "salesforce", "microsoft-365",
    "linkedin", "zoom", "github", "jira", "figma", "hubspot", "canva",
    "linear", "asana", "clickup", "shopify", "tiendanube", "woocommerce",
    "trello", "monday-com", "intercom", "zendesk", "box", "dropbox",
    "docusign", "calendly", "loom", "outreach", "salesloft", "apollo",
    "clay", "zoominfo", "nooks", "stripe", "quickbooks", "netsuite",
    "ramp", "workday", "rippling", "ashby", "greenhouse", "vercel",
    "tableau", "hex", "amplitude", "mixpanel", "snowflake", "databricks",
    "mailchimp"
  ]) assert.ok(ids.includes(id), `missing connector ${id}`);
  assert.deepEqual([...contracts.DIRECT_CONNECTOR_IDS], [
    "salesforce", "docusign", "outreach", "clay", "zoominfo", "netsuite",
    "ramp", "workday", "tableau", "snowflake", "woocommerce"
  ]);
  assert.equal(contracts.HOSTED_CONNECTOR_IDS.length, 44);
});

test("renders a bundled brand logo for every plugin", async () => {
  const [renderer, bundledLogos] = await Promise.all([
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/connector-logo-data.ts", import.meta.url), "utf8")
  ]);
  const simpleIconIds = new Set(
    [...renderer.matchAll(/^  ([a-z0-9]+): si[A-Za-z0-9]+,$/gm)].map((match) => match[1])
  );
  const bundledIconIds = new Set(
    [...bundledLogos.matchAll(/^  "([a-z0-9]+)": "data:image\//gm)].map((match) => match[1])
  );
  const missing = [...new Set(contracts.CONNECTOR_CATALOG.map((item) => item.icon))]
    .filter((iconId) => !simpleIconIds.has(iconId) && !bundledIconIds.has(iconId));
  assert.deepEqual(missing, []);
  assert.match(renderer, /CONNECTOR_LOGO_DATA_URLS\[iconId\]/);
});

test("keeps Electron, the Python broker, and the Pi extension connector ids aligned", async () => {
  const [backend, extension] = await Promise.all([
    readFile(new URL("../go_backend/connectors.py", import.meta.url), "utf8"),
    readFile(new URL("../extensions/connectors/index.ts", import.meta.url), "utf8")
  ]);
  const desktopIds = contracts.CONNECTOR_CATALOG.map((item) => item.id).sort();
  const backendIds = [...backend.matchAll(/_connector\(\s*\n?\s*"([a-z0-9-]+)"/g)].map((match) => match[1]).sort();
  const extensionIds = [...extension.matchAll(/provider\("([a-z0-9-]+)"/g)].map((match) => match[1]).sort();
  assert.deepEqual(backendIds, desktopIds);
  assert.deepEqual(extensionIds, desktopIds);
});

test("ships and starts the account-scoped native Pi Chrome runtime", async () => {
  const [main, oauth, runtime, loader, builder, packageJson] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/local-agent-runtime.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/pi-extension-loader.mjs", import.meta.url), "utf8"),
    readFile(new URL("../electron-builder.yml", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8")
  ]);
  assert.match(main, /new LocalAgentRuntime/);
  assert.match(main, /localAgentRuntime\.start\(\)/);
  assert.match(oauth, /\/v1\/desktop-runtime\/heartbeat/);
  assert.match(oauth, /\/v1\/desktop-runtime\/jobs\/claim/);
  assert.match(oauth, /wait_ms: 20_000/);
  assert.match(oauth, /\/v1\/desktop-runtime\/jobs\/\$\{encodeURIComponent\(jobId\)\}\/complete/);
  assert.match(runtime, /createAgentSession/);
  assert.match(runtime, /noTools: "builtin"/);
  assert.match(runtime, /PI_CHROME_BRIDGE_HOST", "127\.0\.0\.1"/);
  assert.match(runtime, /connectorExtension/);
  assert.match(runtime, /input: \["text"\]/);
  assert.match(runtime, /modelRuntimes = new Map/);
  assert.match(runtime, /resourceLoaders = new Map/);
  assert.match(runtime, /model_runtime_ready_ms/);
  assert.match(runtime, /prompt_complete_ms/);
  assert.match(runtime, /apiKey: `\$\$\{LOCAL_RUN_KEY_ENV\}`/);
  assert.doesNotMatch(runtime, /setRuntimeApiKey/);
  assert.match(loader, /pi-chrome\/extensions\/chrome-profile-bridge/);
  assert.match(loader, /@injaneity\/pi-computer-use/);
  assert.match(builder, /to: pi-runtime\/pi-chrome\/browser-extension/);
  assert.match(builder, /to: pi-runtime\/pi-computer-use/);
  assert.match(packageJson, /"pi-chrome": "0\.15\.46"/);
  assert.match(packageJson, /"@injaneity\/pi-computer-use": "0\.5\.0"/);
});

test("normalizes connector selection and persisted bot state", () => {
  const normalized = contracts.normalizeConnectorIds([
    "slack", "slack", "not-real", "shopify", 42
  ]);
  assert.deepEqual(normalized, ["slack", "shopify"]);

  const state = contracts.normalizeAppState({
    version: 99,
    onboardingCompleted: true,
    selectedConnectorIds: ["github", "not-real"],
    activeBotId: "bot-1",
    bots: [{
      id: "bot-1",
      name: "  Mi   bot  ",
      color: "#2f91f5",
      shape: "circle",
      connectorIds: ["github", "not-real"],
      createdAt: "2026-08-11T00:00:00.000Z"
    }]
  });
  assert.equal(state.version, 2);
  assert.deepEqual(state.selectedConnectorIds, ["github"]);
  assert.equal(state.bots[0].name, "Mi bot");
  assert.deepEqual(state.bots[0].connectorIds, ["github"]);
  assert.deepEqual(state.bots[0].messages, []);
  assert.deepEqual(state.bots[0].workflows, []);
  assert.equal(state.bots[0].title, "");
  assert.equal(state.bots[0].description, "");
  assert.equal(state.bots[0].avatarDataUrl, "");
  assert.equal(state.bots[0].notificationsEnabled, true);
  assert.equal(state.activeBotId, "bot-1");
});

test("collapses a duplicate assistant completion only within the same user turn", () => {
  const normalized = contracts.normalizeAppState({
    bots: [{
      id: "bot-duplicates",
      name: "Asistente",
      color: "#2f91f5",
      shape: "circle",
      createdAt: "2026-08-17T00:00:00.000Z",
      messages: [
        { id: "user-1", role: "user", text: "Hola", createdAt: "2026-08-17T00:00:01.000Z" },
        { id: "assistant-1", role: "assistant", text: "Listo", createdAt: "2026-08-17T00:00:02.000Z" },
        { id: "assistant-2", role: "assistant", text: "Listo", createdAt: "2026-08-17T00:00:03.000Z" },
        { id: "user-2", role: "user", text: "Otra vez", createdAt: "2026-08-17T00:00:04.000Z" },
        { id: "assistant-3", role: "assistant", text: "Listo", createdAt: "2026-08-17T00:00:05.000Z" }
      ]
    }]
  });

  assert.deepEqual(
    normalized.bots[0].messages.map((message) => message.id),
    ["user-1", "assistant-1", "user-2", "assistant-3"]
  );
});

test("normalizes learned workflows inside the account-scoped bot state", () => {
  const workflow = contracts.createBotWorkflow({
    title: "  Publicar   reporte  ",
    summary: "Prepara y publica el reporte semanal.",
    steps: ["Abrir el dashboard", "Exportar los datos", "Compartir el enlace"]
  }, "workflow-1", "6d45fc31-6bc2-4dbe-a6a1-7d11850f3ad4", "video/webm", new Date("2026-08-12T00:00:00.000Z"));
  assert.equal(workflow.title, "Publicar reporte");
  assert.equal(workflow.steps.length, 3);
  assert.equal(workflow.recordingMimeType, "video/webm");

  const state = contracts.normalizeAppState({
    onboardingCompleted: true,
    bots: [{
      id: "bot-workflow",
      name: "Operaciones",
      color: "#2f91f5",
      shape: "circle",
      workflows: [workflow, { id: "invalid", title: "", steps: [] }],
      createdAt: "2026-08-12T00:00:00.000Z"
    }]
  });
  assert.equal(state.bots[0].workflows.length, 1);
  assert.equal(state.bots[0].workflows[0].title, "Publicar reporte");
  assert.throws(() => contracts.createBotWorkflow({ title: "Vacío", summary: "", steps: [] }, "bad", "", ""));
});

test("records teach-task locally but fails closed while the text-only model cannot inspect frames", async () => {
  const [main, preload, oauth, renderer, styles] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/renderer/styles.css", import.meta.url), "utf8")
  ]);
  assert.match(main, /desktop:start-teach-recording/);
  assert.match(main, /desktop:stop-teach-recording/);
  assert.match(main, /desktop:run-bot-workflow/);
  assert.match(main, /setDisplayMediaRequestHandler/);
  assert.match(main, /useSystemPicker:\s*true/);
  assert.match(main, /mode:\s*0o600, flag:\s*"wx"/);
  assert.match(main, /buildWorkflowRunPrompt\(bot, workflow\)/);
  assert.match(main, /computer: true,\s+botId/);
  assert.match(preload, /startTeachRecording/);
  assert.match(preload, /stopTeachRecording/);
  assert.match(oauth, /Teach a task está pausado mientras Agent Genia no tenga soporte visual/);
  assert.doesNotMatch(oauth, /"\/v1\/responses"|type: "input_image"/);
  assert.match(renderer, /navigator\.mediaDevices\.getDisplayMedia/);
  assert.match(renderer, /new MediaRecorder/);
  assert.match(renderer, /Grabando la computadora de \$\{escapeHtml\(bot\.name\)\}/);
  assert.match(renderer, /Detener y guardar/);
  assert.match(renderer, /La grabación de tareas estará disponible cuando vuelva el soporte visual/);
  assert.match(styles, /\.teach-recording-overlay/);
  assert.match(styles, /\.workflow-panel/);
  assert.doesNotMatch(`${main}\n${preload}\n${renderer}`, /go_backend\/pi_harness|from "\.\.\/go_backend/);
});

test("controls one persistent computer per bot through isolated Electron IPC", async () => {
  const [main, preload, oauth, renderer, styles] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/renderer/styles.css", import.meta.url), "utf8")
  ]);
  assert.match(main, /desktop:computer-status/);
  assert.match(main, /desktop:ensure-computer/);
  assert.match(main, /desktop:hand-back-computer/);
  assert.match(main, /rememberComputerViewerUrl\(snapshot\.viewer_url\)/);
  assert.match(main, /issuedComputerViewerUrls\.delete\(url\)/);
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /sandbox:\s*true/);
  assert.match(preload, /computerStatus/);
  assert.match(preload, /openComputerViewer/);
  assert.match(oauth, /\/v1\/computers\/\$\{encodeURIComponent\(botId\)\}\/ensure/);
  assert.match(oauth, /safeComputerViewerUrl/);
  assert.match(renderer, /computer-monitor-strip/);
  assert.match(renderer, /Crear y abrir/);
  assert.match(renderer, /Hibernar/);
  assert.match(styles, /\.computer-monitor-strip/);
  assert.doesNotMatch(`${main}\n${preload}\n${renderer}`, /go_backend\/pi_harness|from "\.\.\/go_backend/);
});

test("validates the generic LLM question widget without hardcoded onboarding content", () => {
  const widget = contracts.normalizeQuestionWidget({
    prompt: "  Pregunta generada  ",
    helpText: "Contexto generado",
    options: Array.from({ length: 8 }, (_item, index) => ({
      label: `Opción ${index + 1}`,
      value: `Quiero la opción ${index + 1}`,
      description: index === 0 ? "Detalle" : ""
    })),
    allowCustom: true,
    dismissOnMoveOn: true
  });
  assert.equal(widget.prompt, "Pregunta generada");
  assert.equal(widget.options.length, 6);
  assert.equal(widget.options[0].value, "Quiero la opción 1");
  assert.equal(contracts.normalizeQuestionWidget({ prompt: "sin opciones", options: [] }), undefined);
});

test("creates bots with bounded validated fields", () => {
  const bot = contracts.createBotProfile({
    name: "   Turno    nocturno   ",
    color: "not-a-color",
    shape: "not-a-shape"
  }, ["slack", "slack", "fake"], "bot-2", new Date("2026-08-11T01:02:03.000Z"));
  assert.equal(bot.name, "Turno nocturno");
  assert.equal(bot.color, contracts.BOT_COLORS[6]);
  assert.equal(bot.shape, contracts.BOT_SHAPES[0]);
  assert.deepEqual(bot.connectorIds, ["slack"]);
  assert.throws(() => contracts.createBotProfile({ name: "", color: "", shape: "" }, [], "bot-3"));
});

test("updates persisted bot personalization without touching its messages", () => {
  const original = contracts.createBotProfile({
    name: "Nuevo bot",
    color: "#2f91f5",
    shape: "circle"
  }, ["slack"], "bot-settings", new Date("2026-08-11T01:02:03.000Z"));
  const updated = contracts.updateBotProfile(original, {
    name: "  Operaciones  ",
    title: "Asistente de operaciones",
    description: "Da seguimiento a pendientes y decisiones.",
    color: "#8654ed",
    shape: "hexagon",
    avatarDataUrl: "data:image/png;base64,iVBORw0KGgo=",
    notificationsEnabled: false
  });
  assert.equal(updated.name, "Operaciones");
  assert.equal(updated.title, "Asistente de operaciones");
  assert.equal(updated.description, "Da seguimiento a pendientes y decisiones.");
  assert.equal(updated.color, "#8654ed");
  assert.equal(updated.shape, "hexagon");
  assert.match(updated.avatarDataUrl, /^data:image\/png;base64,/);
  assert.equal(updated.notificationsEnabled, false);
  assert.deepEqual(updated.messages, original.messages);
  assert.deepEqual(updated.connectorIds, ["slack"]);
  assert.throws(() => contracts.updateBotProfile(original, { name: "" }));
  assert.throws(() => contracts.updateBotProfile(original, { avatarDataUrl: "data:image/svg+xml;base64,PHN2Zz4=" }));
});

test("opens personalization from the bot avatar instead of after bot creation", async () => {
  const renderer = await readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8");
  assert.match(renderer, /class="bot-avatar-trigger"[^>]*data-open-settings/);
  assert.match(renderer, /activeView = "bot-detail";\s+settingsOpen = false;/);
  assert.match(renderer, /function closeBotSettings\(\): void/);
});

test("matches the conversation shell with bot search, plus-only creation, and composer", async () => {
  const renderer = await readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8");
  assert.match(renderer, /class="sidebar-search"/);
  assert.match(renderer, /class="sidebar-new-button"/);
  assert.doesNotMatch(renderer, /class="sidebar-draft/);
  assert.match(renderer, /function botSidebarPreview/);
  assert.match(renderer, /function renderMessageComposer/);
  assert.match(renderer, /\$\{renderMessageComposer\(bot\.name, bot\.id\)\}/);
});

test("keeps the first-bot builder and starts every created bot with the LLM", async () => {
  const renderer = await readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8");
  assert.match(renderer, /async function createDefaultBot\(\): Promise<void>/);
  assert.match(renderer, /if \(!state\.bots\.length\)[\s\S]{0,180}activeView = "bot-builder"/);
  assert.match(renderer, /name: "Nuevo bot",\s+color: BOT_COLORS\[6\],\s+shape: BOT_SHAPES\[0\]/);
  assert.match(renderer, /maybeInitializeBotConversation\(created\.id\)/);
  assert.doesNotMatch(renderer, /skipSetup|renderBotOnboarding|BOT_SETUP_OPTIONS/);
  assert.match(renderer, /\[data-new-bot\][\s\S]{0,150}createDefaultBot\(\)/);
});

test("runs bot conversations through wrapper-backend without hardcoded replies", async () => {
  const [main, preload, oauth, renderer] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  assert.match(main, /desktop:run-bot-agent/);
  assert.match(main, /buildBotPrompt\(\{ \.\.\.bot, connectorIds \}, prompt, initial\)/);
  assert.match(main, /executionMode: initial \? "chat" : "auto"/);
  assert.match(main, /chatPrompt: initial[\s\S]{0,160}\? buildBotPrompt[\s\S]{0,160}: buildDirectChatPrompt/);
  assert.match(main, /const connectorIds = normalizeConnectorIds\(bot\.connectorIds\)/);
  assert.doesNotMatch(main, /\.\.\.before\.selectedConnectorIds/);
  assert.match(oauth, /\$\{this\.options\.baseUrl\}\/v1\/agent\/run/);
  assert.match(oauth, /connector_ids: connectorIds/);
  assert.match(oauth, /Accept: "text\/event-stream"/);
  assert.match(oauth, /stream: true/);
  assert.match(oauth, /eventName === "done64"/);
  assert.match(preload, /runBotAgent/);
  assert.match(preload, /desktop:agent-delta/);
  assert.match(renderer, /desktopApi\.runBotAgent\(botId, message, false, action\)/);
  assert.match(renderer, /option\.action/);
  assert.match(oauth, /approval: options\.approval/);
  assert.doesNotMatch(oauth, /if \(streamedText\) return \{ answer: streamedText/);
  assert.match(renderer, /streamingAssistantText/);
  assert.match(renderer, /desktopApi\.warmBotAgent\(botId\)/);
  assert.match(renderer, /void warmBotAgent\(botId\)/);
  assert.doesNotMatch(renderer, /const warming = agentWarmTasks\.get\(botId\);/);
  assert.match(oauth, /"\/v1\/agent\/warm"/);
  assert.match(main, /normalizeQuestionWidget\(record\.widget\)/);
  assert.match(main, /scheduleRemoteSync\(\)/);
  assert.match(main, /bot\.messages\.slice\(-4\)/);
  assert.match(main, /desktop:refresh-account-state/);
  assert.match(main, /loadRemote: false/);
  assert.match(preload, /refreshAccountState/);
  assert.match(renderer, /window\.addEventListener\("focus", \(\) => void resumeActiveBot\(\)\)/);
  assert.match(renderer, /void refreshConnections\(\)/);
  assert.match(main, /Devuelve exclusivamente JSON válido/);
  assert.match(renderer, /renderGeneratedQuestion/);
  assert.doesNotMatch(`${main}\n${renderer}`, /Hey — I'm New Bot|What should I help with most|Hola, soy \$\{escapeHtml\(bot\.name\)\}/);
});

test("isolates desktop bot state by signed-in account and clears the signed-out view", async () => {
  const [main, renderer, oauth] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8")
  ]);
  assert.match(main, /createHash\("sha256"\)\.update\(accountId\)/);
  assert.match(main, /path\.join\(userDataPath, "accounts"\)/);
  assert.match(main, /legacyFilePath: path\.join\(userDataPath, "desktop-state\.json"\)/);
  assert.match(main, /this\.dirty = loadedDirty \|\| Boolean\(migratedLegacyFilePath\)/);
  assert.match(main, /await stateStore\.activateAccount\(null\)/);
  assert.match(main, /claimGuest: wasSignedOut/);
  assert.match(main, /desktop-state\.json\.migrated|\$\{migratedLegacyFilePath\}\.migrated/);
  assert.match(oauth, /async accountId\(\): Promise<string \| null>/);
  assert.match(renderer, /connections = await desktopApi\.signOut\(\);\s+state = await desktopApi\.bootstrap\(\)/);
  assert.doesNotMatch(main, /new DesktopStateStore\(path\.join\(userDataPath, "desktop-state\.json"\)\)/);
});

test("opens a post-onboarding plugin marketplace and derives Yours from installed connectors", async () => {
  const renderer = await readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8");
  assert.match(renderer, /type View = "connectors" \| "plugins"/);
  assert.match(renderer, /data-plugin-tab="marketplace"/);
  assert.match(renderer, /data-plugin-tab="yours"/);
  assert.match(renderer, /CONNECTOR_CATALOG\.filter\(\(connector\) => selectedConnectorIds\.has\(connector\.id\)\)/);
  assert.match(renderer, /\[data-open-connectors\][\s\S]{0,180}activeView = "plugins"/);
  assert.doesNotMatch(renderer, /\[data-open-connectors\][\s\S]{0,180}activeView = "connectors"/);
});

test("keeps Electron renderer isolated from Node and external network", async () => {
  const [main, preload, html] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/renderer/index.html", import.meta.url), "utf8")
  ]);
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /nodeIntegration:\s*false/);
  assert.match(main, /sandbox:\s*true/);
  assert.match(preload, /contextBridge\.exposeInMainWorld\("wrapperDesktop"/);
  assert.match(html, /connect-src 'none'/);
});

test("opens Stripe Checkout and the customer portal only through isolated IPC", async () => {
  const [main, preload, oauth, renderer] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  assert.match(main, /desktop:start-checkout/);
  assert.match(main, /tier !== "basic" && tier !== "pro" && tier !== "business"/);
  assert.match(preload, /startCheckout/);
  assert.match(preload, /openBillingPortal/);
  assert.doesNotMatch(preload, /STRIPE_SECRET_KEY|sk_live_|whsec_/);
  assert.match(oauth, /safeStripeUrl/);
  assert.match(oauth, /checkout\.stripe\.com/);
  assert.match(oauth, /billing\.stripe\.com/);
  assert.match(renderer, /function renderBilling\(\)/);
  assert.match(renderer, /data-select-plan/);
  assert.match(renderer, /data-open-billing-portal/);
  assert.match(renderer, /Starter/);
  assert.match(renderer, /Business/);
  assert.match(oauth, /idempotency_key: options\.idempotencyKey \?\? randomUUID\(\)/);
  assert.match(oauth, /max_credits: 15/);
});

test("links the signed-in account to official WhatsApp without exposing server secrets", async () => {
  const [main, preload, oauth, renderer, styles] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/renderer/styles.css", import.meta.url), "utf8")
  ]);
  assert.match(main, /desktop:start-whatsapp-link/);
  assert.match(main, /desktop:unlink-whatsapp/);
  assert.match(preload, /startWhatsAppLink/);
  assert.doesNotMatch(preload, /WHATSAPP_ACCESS_TOKEN|WHATSAPP_APP_SECRET|WHATSAPP_VERIFY_TOKEN/);
  assert.match(oauth, /"\/v1\/whatsapp\/link"/);
  assert.match(oauth, /"\/v1\/whatsapp\/status"/);
  assert.match(oauth, /safeWhatsAppUrl/);
  assert.match(oauth, /url\.hostname !== "wa\.me"/);
  assert.match(renderer, /Usa tus agentes desde WhatsApp/);
  assert.match(renderer, /No necesitas una cuenta de WhatsApp Business/);
  assert.match(renderer, /scheduleWhatsAppPoll/);
  assert.match(styles, /\.whatsapp-account-card/);
});

test("deletes the signed-in account and local per-account state through isolated IPC", async () => {
  const [main, preload, oauth, renderer] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  assert.match(main, /desktop:delete-account/);
  assert.match(main, /stateStore\.deleteActiveAccount\(\)/);
  assert.match(main, /teachRecordingsDirectory, accountScope\(accountId\)/);
  assert.match(main, /Promise\.allSettled/);
  assert.match(preload, /deleteAccount/);
  assert.match(oauth, /"\/v1\/account\/delete"/);
  assert.match(oauth, /confirmation: "DELETE"/);
  assert.match(oauth, /deviceStore\.clear\(\)/);
  assert.match(renderer, /data-delete-account/);
  assert.match(renderer, /window\.confirm/);
  assert.doesNotMatch(preload, /\/v1\/account\/delete/);
});

test("stores real OAuth sessions outside the renderer and binds them to one signed-in account", async () => {
  const [oauth, main, preload, renderer] = await Promise.all([
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  assert.match(oauth, /safeStorage\.encryptStringAsync/);
  assert.match(oauth, /agent-genia-account\.bin/);
  assert.doesNotMatch(oauth, /agent-genia-connectors-account\.bin/);
  assert.match(oauth, /WrapperServiceClient/);
  assert.match(oauth, /this\.publicJson\("\/v1\/account-auth\/status",\s*\{\s*method: "POST",\s*body: \{ attempt_id: attemptId, device_id: deviceId \}/);
  assert.doesNotMatch(oauth, /account-auth\/status\/\$\{encodeURIComponent\(attemptId\)\}/);
  assert.match(main, /WRAPPER_SERVICE_URL\?\.trim\(\)/);
  assert.match(main, /https:\/\/agentgenia-api\.onrender\.com/);
  assert.match(oauth, /managed_connection_id/);
  assert.match(oauth, /"\/v1\/connectors"/);
  assert.match(oauth, /\/v1\/connectors\/start/);
  assert.match(oauth, /COMPOSIO_CONNECTOR_IDS/);
  assert.match(oauth, /baseUrl/);
  assert.match(main, /DesktopOAuthController/);
  assert.match(main, /WRAPPER_SERVICE_URL/);
  assert.doesNotMatch(main, /OUTCOME_SERVICE_URL|outcome-service/);
  assert.doesNotMatch(oauth, /OutcomeOAuthClient|ManagedProviderSession|connector-managed-|owner_account_id|access_token/);
  assert.match(preload, /connectConnector/);
  assert.doesNotMatch(preload, /access_token|refresh_token|client_secret/);
  assert.match(renderer, /Próximamente/);
  assert.match(renderer, /data-connect-connector/);
});

test("desktop layer does not import or rewrite the Pi harness", async () => {
  const files = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  for (const source of files) assert.doesNotMatch(source, /pi_harness|go_backend/);
});

test("keeps account settings visible while a long bot list scrolls independently", async () => {
  const styles = await readFile(new URL("../desktop/renderer/styles.css", import.meta.url), "utf8");
  assert.match(styles, /\.desktop-sidebar \{[^}]*min-height: 0;[^}]*height: 100%;[^}]*overflow: hidden;/);
  assert.match(styles, /\.sidebar-nav \{[^}]*flex: 1 1 auto;[^}]*min-height: 0;[^}]*overflow-y: auto;/);
  assert.match(styles, /\.sidebar-footer \{[^}]*flex: 0 0 auto;/);
});

test("resolves Electron build inputs as native paths on Windows", async () => {
  const buildScript = await readFile(new URL("../desktop/build.mjs", import.meta.url), "utf8");
  assert.match(buildScript, /fileURLToPath/);
  assert.doesNotMatch(buildScript, /new URL\([^\n]+\)\.pathname/);
});

test("keeps one normal Electron main process so renderer and IPC handlers cannot drift", async () => {
  const main = await readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8");
  assert.match(main, /smokeTest \|\| app\.requestSingleInstanceLock\(\)/);
  assert.match(main, /app\.on\("second-instance"/);
  assert.match(main, /mainWindow\.focus\(\)/);
  assert.match(main, /if \(hasSingleInstanceLock\) app\.whenReady\(\)/);
});

test("runs packaged smoke tests with isolated temporary user data on every desktop OS", async () => {
  const [main, oauth, workflow] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../.github/workflows/desktop.yml", import.meta.url), "utf8")
  ]);
  assert.match(main, /mkdtempSync\(path\.join\(tmpdir\(\), "agentgenia-smoke-"\)\)/);
  assert.match(main, /app\.setPath\("userData", smokeUserDataPath\)/);
  assert.match(main, /Create the first BrowserWindow before touching Keychain-backed session/);
  const windowCreated = main.indexOf("createWindow();");
  const rendererLoaded = main.indexOf('did-finish-load', windowCreated);
  const accountUnlocked = main.indexOf("oauthController.accountId()", rendererLoaded);
  assert.ok(windowCreated >= 0 && rendererLoaded > windowCreated && accountUnlocked > rendererLoaded);
  assert.match(main, /new LocalAgentRuntime\(/);
  assert.match(main, /localAgentRuntime\.start\(\)/);
  assert.match(oauth, /safeStorage\.isAsyncEncryptionAvailable/);
  assert.match(workflow, /Launch packaged macOS application/);
  assert.match(workflow, /Launch packaged Windows application/);
  assert.match(workflow, /Launch packaged Linux application/);
});
