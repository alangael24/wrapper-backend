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
  assert.equal(state.version, 1);
  assert.deepEqual(state.selectedConnectorIds, ["github"]);
  assert.equal(state.bots[0].name, "Mi bot");
  assert.deepEqual(state.bots[0].connectorIds, ["github"]);
  assert.deepEqual(state.bots[0].messages, []);
  assert.equal(state.bots[0].title, "");
  assert.equal(state.bots[0].description, "");
  assert.equal(state.bots[0].avatarDataUrl, "");
  assert.equal(state.bots[0].notificationsEnabled, true);
  assert.equal(state.activeBotId, "bot-1");
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
  assert.match(renderer, /initializeBotConversation\(created\.id\)/);
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
  assert.match(main, /\.\.\.before\.selectedConnectorIds/);
  assert.match(main, /\.\.\.bot\.connectorIds/);
  assert.match(oauth, /"\/v1\/agent\/run"/);
  assert.match(oauth, /connector_ids: connectorIds/);
  assert.match(preload, /runBotAgent/);
  assert.match(renderer, /desktopApi\.runBotAgent\(botId, message\)/);
  assert.match(main, /normalizeQuestionWidget\(record\.widget\)/);
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
  assert.match(main, /tier !== "basic" && tier !== "pro"/);
  assert.match(preload, /startCheckout/);
  assert.match(preload, /openBillingPortal/);
  assert.doesNotMatch(preload, /STRIPE_SECRET_KEY|sk_live_|whsec_/);
  assert.match(oauth, /safeStripeUrl/);
  assert.match(oauth, /checkout\.stripe\.com/);
  assert.match(oauth, /billing\.stripe\.com/);
  assert.match(renderer, /function renderBilling\(\)/);
  assert.match(renderer, /data-select-plan/);
  assert.match(renderer, /data-open-billing-portal/);
});

test("stores real OAuth sessions outside the renderer and binds them to one signed-in account", async () => {
  const [oauth, main, preload, renderer] = await Promise.all([
    readFile(new URL("../desktop/src/oauth.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  assert.match(oauth, /safeStorage\.encryptString/);
  assert.match(oauth, /agent-genia-account\.bin/);
  assert.doesNotMatch(oauth, /agent-genia-connectors-account\.bin/);
  assert.match(oauth, /WrapperServiceClient/);
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

test("keeps one normal Electron main process so renderer and IPC handlers cannot drift", async () => {
  const main = await readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8");
  assert.match(main, /smokeTest \|\| app\.requestSingleInstanceLock\(\)/);
  assert.match(main, /app\.on\("second-instance"/);
  assert.match(main, /mainWindow\.focus\(\)/);
  assert.match(main, /if \(hasSingleInstanceLock\) app\.whenReady\(\)/);
});
