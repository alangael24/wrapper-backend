import {
  siAsana,
  siBox,
  siCalendly,
  siCanva,
  siClickup,
  siDatabricks,
  siDropbox,
  siFigma,
  siGithub,
  siGoogle,
  siGreenhouse,
  siHubspot,
  siIntercom,
  siJira,
  siLinear,
  siLoom,
  siMailchimp,
  siMixpanel,
  siNotion,
  siQuickbooks,
  siSalesforce,
  siShopify,
  siSlack,
  siSnowflake,
  siStripe,
  siTrello,
  siVercel,
  siWoocommerce,
  siZendesk,
  siZoom,
  type SimpleIcon
} from "simple-icons";
import {
  BOT_COLORS,
  BOT_SETUP_OPTIONS,
  BOT_SHAPES,
  BOT_TEMPLATES,
  CONNECTOR_CATALOG,
  HOSTED_CONNECTOR_IDS,
  type AppState,
  type BillingSnapshot,
  type BotDraft,
  type BotPatch,
  type BotProfile,
  type BotSetupAnswer,
  type ConnectorConnectionSnapshot,
  type DesktopApi,
  applyBotSetupAnswer,
  createBotProfile,
  initialAppState,
  normalizeConnectorIds,
  updateBotProfile
} from "./contracts";
import { CONNECTOR_LOGO_DATA_URLS } from "./connector-logo-data";

declare global {
  interface Window {
    wrapperDesktop?: DesktopApi;
  }
}

type View = "connectors" | "plugins" | "billing" | "bot-builder" | "bot-detail";

const CONNECTOR_CATEGORIES = Object.freeze([
  "Trabajo", "Ventas", "Soporte", "Desarrollo", "Diseño",
  "Finanzas", "RR. HH.", "Datos", "Marketing", "Comercio"
]);

const appRootElement = document.querySelector<HTMLDivElement>("#app");
if (!appRootElement) throw new Error("No se encontró la raíz de la aplicación.");
const appRoot: HTMLDivElement = appRootElement;

const iconCatalog: Record<string, SimpleIcon> = {
  google: siGoogle,
  slack: siSlack,
  notion: siNotion,
  salesforce: siSalesforce,
  zoom: siZoom,
  github: siGithub,
  jira: siJira,
  figma: siFigma,
  hubspot: siHubspot,
  canva: siCanva,
  linear: siLinear,
  asana: siAsana,
  clickup: siClickup,
  trello: siTrello,
  intercom: siIntercom,
  zendesk: siZendesk,
  box: siBox,
  dropbox: siDropbox,
  calendly: siCalendly,
  loom: siLoom,
  stripe: siStripe,
  quickbooks: siQuickbooks,
  greenhouse: siGreenhouse,
  vercel: siVercel,
  mixpanel: siMixpanel,
  snowflake: siSnowflake,
  databricks: siDatabricks,
  mailchimp: siMailchimp,
  shopify: siShopify,
  woocommerce: siWoocommerce,
};

let state = initialAppState();
let activeView: View = "connectors";
let connectorQuery = "";
let pluginQuery = "";
let sidebarQuery = "";
let pluginTab: "marketplace" | "yours" = "marketplace";
let selectedConnectorIds = new Set<string>();
let botDraft: BotDraft = { name: "", color: "#08be70", shape: "drop" };
let transientError = "";
let settingsOpen = false;
let avatarEditorOpen = false;
let avatarEditorTab: "bot" | "generate" | "upload" = "bot";
let connections: ConnectorConnectionSnapshot = emptyConnectionSnapshot();
let authBusyConnectorId = "";
let accountAuthBusy = false;
let billing = emptyBillingSnapshot();
let billingLoaded = false;
let billingBusy = false;
let billingNotice = "";
const settingsSaveTimers = new Map<string, number>();

const desktopApi = window.wrapperDesktop ?? createPreviewApi();

void initialize();

async function initialize(): Promise<void> {
  try {
    [state, connections] = await Promise.all([
      desktopApi.bootstrap(),
      desktopApi.connectionSnapshot()
    ]);
    selectedConnectorIds = new Set(state.selectedConnectorIds);
    activeView = state.onboardingCompleted ? (state.bots.length ? "bot-detail" : "bot-builder") : "connectors";
    const preview = new URLSearchParams(window.location.search).get("preview");
    if (!window.wrapperDesktop && preview === "plugins") {
      selectedConnectorIds = new Set(["google-workspace", "slack", "github"]);
      state = { ...state, onboardingCompleted: true, selectedConnectorIds: [...selectedConnectorIds] };
      activeView = "plugins";
    } else if (!window.wrapperDesktop && preview === "bot") {
      selectedConnectorIds = new Set(["google-workspace", "slack", "notion", "shopify"]);
      state = { ...state, onboardingCompleted: true, selectedConnectorIds: [...selectedConnectorIds] };
      activeView = "bot-builder";
    } else if (!window.wrapperDesktop && ["setup", "connections", "settings", "settings-avatar"].includes(preview ?? "")) {
      selectedConnectorIds = new Set(["google-workspace", "slack"]);
      let bot = createBotProfile({ name: "Juan", color: "#2f91f5", shape: "drop" }, [...selectedConnectorIds], "preview-bot");
      if (preview === "connections") {
        bot = applyBotSetupAnswer(bot, { step: "purpose", value: "work" });
        bot = applyBotSetupAnswer(bot, { step: "workspace", value: "mix" });
        bot = applyBotSetupAnswer(bot, { step: "project", value: "notion" });
      }
      state = { ...state, onboardingCompleted: true, selectedConnectorIds: [...selectedConnectorIds], bots: [bot], activeBotId: bot.id };
      activeView = "bot-detail";
      settingsOpen = preview === "settings" || preview === "settings-avatar";
      avatarEditorOpen = preview === "settings-avatar";
    }
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function render(): void {
  if (activeView === "connectors") renderConnectors();
  else if (activeView === "plugins") renderPluginMarketplace();
  else if (activeView === "billing") renderBilling();
  else if (activeView === "bot-builder") renderBotBuilder();
  else renderBotDetail();
}

function renderConnectors(): void {
  const normalizedQuery = connectorQuery.trim().toLocaleLowerCase("es");
  const visible = CONNECTOR_CATALOG.filter((connector) => (
    !normalizedQuery
    || `${connector.name} ${connector.category} ${connector.description}`.toLocaleLowerCase("es").includes(normalizedQuery)
  ));
  const groups = CONNECTOR_CATEGORIES;

  appRoot.innerHTML = `
    <main class="connector-screen">
      <div class="floating-mascot floating-mascot-left">${renderMascot("circle", "#18bda7", "small")}</div>
      <div class="floating-mascot floating-mascot-right">${renderMascot("circle", "#2f91f5", "small")}</div>
      <header class="connector-heading">
        <span class="eyebrow">CONECTA TU FLUJO</span>
        <h1>¿Qué usas todos los días?</h1>
        <p>Elige las herramientas y autoriza tu propia cuenta. Las sesiones quedan cifradas en este dispositivo y nunca se comparten con otros usuarios.</p>
      </header>
      ${renderAccountBanner()}
      <label class="connector-search">
        <span aria-hidden="true">⌕</span>
        <input id="connector-search" type="search" placeholder="Buscar herramientas" value="${escapeAttribute(connectorQuery)}" autocomplete="off" />
        <small>${selectedConnectorIds.size} elegida${selectedConnectorIds.size === 1 ? "" : "s"}</small>
      </label>
      <section class="connector-groups" aria-label="Catálogo de conectores">
        ${groups.map((group) => {
          const items = visible.filter((connector) => connector.category === group);
          if (!items.length) return "";
          return `
            <div class="connector-group">
              <div class="connector-group-title"><strong>${group}</strong><span>${items.length}</span></div>
              <div class="connector-grid">
                ${items.map((connector) => {
                  const selected = selectedConnectorIds.has(connector.id);
                  const connection = connectorConnection(connector.id);
                  return `
                    <article class="connector-card${selected ? " selected" : ""}${connection.connected ? " connected" : ""}">
                      <button class="connector-card-main" type="button" data-connector-id="${connector.id}" aria-pressed="${selected}">
                        ${renderConnectorIcon(connector.icon, connector.name)}
                        <span class="connector-card-copy"><strong>${connector.name}</strong><small>${connector.description}</small></span>
                        <span class="connector-check" aria-hidden="true">${selected ? "✓" : "+"}</span>
                      </button>
                      ${renderConnectorAuthAction(connector.id)}
                    </article>`;
                }).join("")}
              </div>
            </div>`;
        }).join("")}
        ${visible.length ? "" : '<div class="empty-search"><strong>No encontramos esa herramienta.</strong><span>Prueba con otro nombre o categoría.</span></div>'}
      </section>
      ${renderError()}
      <footer class="connector-actions">
        <button id="connectors-next" class="primary-action" type="button">Siguiente</button>
        <button id="connectors-back" class="secondary-action" type="button">${state.bots.length ? "Volver a mis bots" : "Omitir por ahora"}</button>
      </footer>
    </main>`;

  document.querySelector<HTMLInputElement>("#connector-search")?.addEventListener("input", (event) => {
    connectorQuery = (event.currentTarget as HTMLInputElement).value;
    renderConnectors();
    const input = document.querySelector<HTMLInputElement>("#connector-search");
    input?.focus();
    input?.setSelectionRange(connectorQuery.length, connectorQuery.length);
  });
  for (const card of document.querySelectorAll<HTMLButtonElement>("[data-connector-id]")) {
    card.addEventListener("click", () => {
      const id = card.dataset.connectorId;
      if (!id) return;
      if (selectedConnectorIds.has(id)) selectedConnectorIds.delete(id);
      else selectedConnectorIds.add(id);
      renderConnectors();
    });
  }
  bindConnectionActions();
  bindAccountActions();
  document.querySelector("#connectors-next")?.addEventListener("click", () => void leaveConnectors("bot-builder"));
  document.querySelector("#connectors-back")?.addEventListener("click", () => void leaveConnectors(state.bots.length ? "bot-detail" : "bot-builder"));
}

function renderAccountBanner(): string {
  if (connections.account.connected) {
    return `
      <section class="account-banner connected">
        <span class="account-avatar">${escapeHtml((connections.account.name || connections.account.email || "A").slice(0, 1).toUpperCase())}</span>
        <span><strong>Sesión personal activa</strong><small>${escapeHtml(connections.account.email)}</small></span>
        <button type="button" data-sign-out>Salir</button>
      </section>`;
  }
  return `
    <section class="account-banner">
      <span class="account-avatar">A</span>
      <span><strong>Conecta tus propias cuentas</strong><small>Inicia sesión una vez y después autoriza cada proveedor.</small></span>
      <button type="button" data-sign-in ${accountAuthBusy ? "disabled" : ""}>${accountAuthBusy ? "Abriendo…" : "Iniciar sesión"}</button>
    </section>`;
}

function connectorConnection(connectorId: string) {
  return connections.connectors.find((item) => item.connectorId === connectorId) ?? {
    connectorId,
    provider: null,
    available: false,
    connected: false,
    account: "",
    reason: "OAuth no configurado."
  };
}

function renderConnectorAuthAction(connectorId: string): string {
  const connection = connectorConnection(connectorId);
  if (!connection.available) {
    return `<span class="connector-auth unavailable" title="${escapeAttribute(connection.reason)}">Próximamente</span>`;
  }
  if (connection.connected) {
    return `<button class="connector-auth connected" type="button" data-disconnect-connector="${connectorId}" title="Desconectar ${escapeAttribute(connection.account)}">✓ Conectado</button>`;
  }
  const busy = authBusyConnectorId === connectorId;
  return `<button class="connector-auth" type="button" data-connect-connector="${connectorId}" ${busy ? "disabled" : ""}>${busy ? "Esperando…" : "Conectar"}</button>`;
}

function bindConnectionActions(): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-connect-connector]")) {
    button.addEventListener("click", () => void connectConnector(button.dataset.connectConnector ?? ""));
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-disconnect-connector]")) {
    button.addEventListener("click", () => void disconnectConnector(button.dataset.disconnectConnector ?? ""));
  }
}

function bindAccountActions(): void {
  document.querySelector("[data-sign-in]")?.addEventListener("click", () => void signInAccount());
  document.querySelector("[data-sign-out]")?.addEventListener("click", () => void signOutAccount());
}

async function signInAccount(): Promise<void> {
  accountAuthBusy = true;
  transientError = "";
  render();
  try {
    connections = await desktopApi.signIn();
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    accountAuthBusy = false;
  }
  render();
}

async function signOutAccount(): Promise<void> {
  transientError = "";
  accountAuthBusy = true;
  render();
  try {
    connections = await desktopApi.signOut();
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    accountAuthBusy = false;
  }
  render();
}

async function connectConnector(connectorId: string): Promise<void> {
  if (!connectorId || authBusyConnectorId) return;
  authBusyConnectorId = connectorId;
  transientError = "";
  render();
  try {
    connections = await desktopApi.connectConnector(connectorId);
    selectedConnectorIds.add(connectorId);
    state = await desktopApi.saveConnectors([...selectedConnectorIds], state.onboardingCompleted);
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    authBusyConnectorId = "";
  }
  render();
}

async function disconnectConnector(connectorId: string): Promise<void> {
  if (!connectorId || authBusyConnectorId) return;
  authBusyConnectorId = connectorId;
  transientError = "";
  render();
  try {
    connections = await desktopApi.disconnectConnector(connectorId);
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    authBusyConnectorId = "";
  }
  render();
}

function renderPluginMarketplace(): void {
  const normalizedQuery = pluginQuery.trim().toLocaleLowerCase("es");
  const installed = CONNECTOR_CATALOG.filter((connector) => selectedConnectorIds.has(connector.id));
  const source = pluginTab === "yours" ? installed : [...CONNECTOR_CATALOG];
  const visible = source.filter((connector) => (
    !normalizedQuery
    || `${connector.name} ${connector.category} ${connector.description}`.toLocaleLowerCase("es").includes(normalizedQuery)
  ));
  const groups = pluginTab === "marketplace"
    ? CONNECTOR_CATEGORIES
    : ["Tus plugins"];

  appRoot.innerHTML = `
    <main class="plugin-marketplace">
      <header class="plugin-marketplace-header">
        <h1>Plugins</h1>
        <button type="button" data-close-plugins aria-label="Cerrar">×</button>
      </header>
      <section class="plugin-toolbar">
        <nav class="plugin-tabs" aria-label="Secciones de plugins">
          <button type="button" data-plugin-tab="marketplace" class="${pluginTab === "marketplace" ? "selected" : ""}">Marketplace</button>
          <button type="button" data-plugin-tab="yours" class="${pluginTab === "yours" ? "selected" : ""}">Yours <span>${installed.length}</span></button>
        </nav>
        <label class="plugin-search"><span>⌕</span><input type="search" id="plugin-search" placeholder="Buscar plugins" value="${escapeAttribute(pluginQuery)}" /></label>
      </section>
      ${renderAccountBanner()}
      <section class="plugin-list" aria-label="${pluginTab === "yours" ? "Plugins instalados" : "Marketplace de plugins"}">
        ${groups.map((group) => {
          const items = pluginTab === "marketplace"
            ? visible.filter((connector) => connector.category === group)
            : visible;
          if (!items.length) return "";
          return `
            <section class="plugin-section">
              <h2>${group}</h2>
              <div class="plugin-grid">
                ${items.map((connector) => renderPluginRow(connector.id)).join("")}
              </div>
            </section>`;
        }).join("")}
        ${visible.length ? "" : `<div class="plugin-empty"><strong>${pluginTab === "yours" ? "Todavía no tienes plugins instalados" : "No encontramos ese plugin"}</strong><span>${pluginTab === "yours" ? "Ve a Marketplace y presiona Add para instalar uno." : "Prueba con otro nombre o categoría."}</span></div>`}
      </section>
      ${renderError()}
    </main>`;

  document.querySelector("[data-close-plugins]")?.addEventListener("click", () => {
    activeView = state.bots.length ? "bot-detail" : "bot-builder";
    render();
  });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-plugin-tab]")) {
    button.addEventListener("click", () => {
      pluginTab = button.dataset.pluginTab === "yours" ? "yours" : "marketplace";
      pluginQuery = "";
      renderPluginMarketplace();
    });
  }
  document.querySelector<HTMLInputElement>("#plugin-search")?.addEventListener("input", (event) => {
    pluginQuery = (event.currentTarget as HTMLInputElement).value;
    renderPluginMarketplace();
    const input = document.querySelector<HTMLInputElement>("#plugin-search");
    input?.focus();
    input?.setSelectionRange(pluginQuery.length, pluginQuery.length);
  });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-install-plugin]")) {
    button.addEventListener("click", () => void setPluginInstalled(button.dataset.installPlugin ?? "", true));
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-remove-plugin]")) {
    button.addEventListener("click", () => void setPluginInstalled(button.dataset.removePlugin ?? "", false));
  }
  bindConnectionActions();
  bindAccountActions();
}

function renderPluginRow(connectorId: string): string {
  const connector = CONNECTOR_CATALOG.find((item) => item.id === connectorId);
  if (!connector) return "";
  const installed = selectedConnectorIds.has(connector.id);
  const connection = connectorConnection(connector.id);
  const secondary = pluginTab === "yours" && installed
    ? connection.available
      ? renderConnectorAuthAction(connector.id)
      : '<span class="plugin-local-status">Instalado localmente</span>'
    : "";
  const primary = pluginTab === "marketplace"
    ? installed
      ? '<span class="plugin-added">✓ Added</span>'
      : `<button type="button" class="plugin-add-button" data-install-plugin="${connector.id}">Add</button>`
    : `<button type="button" class="plugin-remove-button" data-remove-plugin="${connector.id}">Remove</button>`;
  return `
    <article class="plugin-row${installed ? " installed" : ""}">
      ${renderConnectorIcon(connector.icon, connector.name)}
      <span class="plugin-row-copy"><strong>${connector.name}</strong><small>${connector.description}</small></span>
      <span class="plugin-row-actions">${secondary}${primary}</span>
    </article>`;
}

async function setPluginInstalled(connectorId: string, installed: boolean): Promise<void> {
  if (!CONNECTOR_CATALOG.some((connector) => connector.id === connectorId)) return;
  transientError = "";
  if (installed) selectedConnectorIds.add(connectorId);
  else selectedConnectorIds.delete(connectorId);
  try {
    state = await desktopApi.saveConnectors([...selectedConnectorIds], state.onboardingCompleted);
  } catch (error) {
    if (installed) selectedConnectorIds.delete(connectorId);
    else selectedConnectorIds.add(connectorId);
    transientError = errorMessage(error);
  }
  renderPluginMarketplace();
}

async function leaveConnectors(nextView: View): Promise<void> {
  setBusy(true);
  transientError = "";
  try {
    state = await desktopApi.saveConnectors([...selectedConnectorIds], true);
    activeView = nextView;
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function renderBilling(): void {
  const tier = billing.tier;
  const subscription = billing.subscription;
  const currentLabel = tier === "basic" ? "Plus" : tier === "pro" ? "Pro" : "Free";
  appRoot.innerHTML = renderDesktopShell(`
    <section class="billing-view">
      <header class="workspace-topbar">
        <span class="billing-mark">$</span>
        <strong>Plan y facturación</strong>
        <button class="topbar-link" type="button" data-refresh-billing ${billingBusy ? "disabled" : ""}>Actualizar</button>
      </header>
      <div class="billing-scroll">
        <div class="billing-heading">
          <span class="eyebrow">AGENTGENIA</span>
          <h1>Elige el plan que acompaña tu trabajo</h1>
          <p>Los cobros y datos de tarjeta se procesan directamente en Stripe. Agentgenia activa el acceso únicamente después de recibir un webhook firmado.</p>
        </div>
        ${!connections.account.connected ? `
          <section class="billing-signin-card">
            <strong>Inicia sesión para administrar tu plan</strong>
            <p>Tu suscripción se vincula a tu cuenta verificada de Agentgenia.</p>
            <button class="primary-action compact" type="button" data-billing-sign-in>Iniciar sesión</button>
          </section>` : !billingLoaded ? `
          <section class="billing-signin-card"><strong>Cargando tus planes…</strong></section>` : `
          <div class="billing-current">
            <span><small>Plan actual</small><strong>${currentLabel}</strong></span>
            ${subscription ? `<span><small>Estado</small><strong>${escapeHtml(subscription.status)}</strong></span>` : ""}
            ${subscription?.cancel_at_period_end ? '<em>Se cancelará al finalizar el periodo actual.</em>' : ""}
            ${billing.customer ? '<button type="button" class="secondary-action" data-open-billing-portal>Administrar en Stripe</button>' : ""}
          </div>
          <div class="billing-plans">
            ${renderBillingPlan("free", "Free", 0, "Para explorar Agentgenia", ["Crea y personaliza bots", "Conecta tus herramientas", "Sin acceso al modelo incluido"], tier)}
            ${renderBillingPlan("basic", billing.plans.basic.name, billing.plans.basic.amount, "Para uso individual", ["Acceso al modelo incluido", "50% de la capacidad de uso", "Portal de facturación y cancelación"], tier)}
            ${renderBillingPlan("pro", billing.plans.pro.name, billing.plans.pro.amount, "Para trabajo intensivo", ["Acceso al modelo incluido", "100% de la capacidad de uso", "Portal de facturación y cancelación"], tier)}
          </div>`}
        ${billingNotice ? `<p class="billing-notice">${escapeHtml(billingNotice)}</p>` : ""}
        ${!billing.configured && billingLoaded && connections.account.connected ? '<p class="inline-error">El servicio de pagos todavía no está habilitado en producción.</p>' : renderError()}
      </div>
    </section>
  `, "billing");
  bindSidebar();
  document.querySelector("[data-refresh-billing]")?.addEventListener("click", () => void refreshBilling());
  document.querySelector("[data-billing-sign-in]")?.addEventListener("click", async () => {
    await signInAccount();
    if (connections.account.connected) await refreshBilling();
  });
  document.querySelector("[data-open-billing-portal]")?.addEventListener("click", () => void openBillingPortal());
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-select-plan]")) {
    button.addEventListener("click", () => void startCheckout(button.dataset.selectPlan as "basic" | "pro"));
  }
}

function renderBillingPlan(
  id: "free" | "basic" | "pro",
  name: string,
  amount: number,
  subtitle: string,
  features: string[],
  currentTier: string
): string {
  const current = id === currentTier;
  const paid = id === "basic" || id === "pro";
  return `
    <article class="billing-plan${id === "pro" ? " featured" : ""}${current ? " current" : ""}">
      <span class="plan-kicker">${id === "pro" ? "MÁS CAPACIDAD" : current ? "TU PLAN" : "MENSUAL"}</span>
      <h2>${escapeHtml(name)}</h2>
      <p>${escapeHtml(subtitle)}</p>
      <div class="plan-price"><strong>$${amount}</strong><span>${amount ? "USD / mes" : "para siempre"}</span></div>
      <ul>${features.map((feature) => `<li>✓ ${escapeHtml(feature)}</li>`).join("")}</ul>
      ${current
        ? '<button type="button" disabled>Plan actual</button>'
        : paid
          ? `<button type="button" data-select-plan="${id}" ${!billing.configured || billingBusy ? "disabled" : ""}>Elegir ${escapeHtml(name)}</button>`
          : '<button type="button" disabled>Incluido</button>'}
    </article>`;
}

async function openBillingView(): Promise<void> {
  closeBotSettings();
  activeView = "billing";
  transientError = "";
  billingNotice = "";
  render();
  if (connections.account.connected) await refreshBilling();
}

async function refreshBilling(): Promise<void> {
  if (!connections.account.connected || billingBusy) return;
  billingBusy = true;
  transientError = "";
  render();
  try {
    billing = await desktopApi.billingSnapshot();
    billingLoaded = true;
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    billingBusy = false;
  }
  render();
}

async function startCheckout(tier: "basic" | "pro"): Promise<void> {
  if (billingBusy) return;
  billingBusy = true;
  transientError = "";
  billingNotice = "";
  render();
  try {
    await desktopApi.startCheckout(tier);
    billingNotice = "Abrimos el Checkout seguro de Stripe en tu navegador. Al terminar, vuelve aquí y presiona Actualizar.";
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    billingBusy = false;
  }
  render();
}

async function openBillingPortal(): Promise<void> {
  if (billingBusy) return;
  billingBusy = true;
  transientError = "";
  render();
  try {
    await desktopApi.openBillingPortal();
    billingNotice = "Abrimos tu portal de Stripe para actualizar el método de pago, cambiar o cancelar el plan.";
  } catch (error) {
    transientError = errorMessage(error);
  } finally {
    billingBusy = false;
  }
  render();
}

function renderBotBuilder(): void {
  appRoot.innerHTML = renderDesktopShell(`
    <section class="bot-builder-view">
      <header class="workspace-topbar">
        <span>${renderMascot(botDraft.shape, botDraft.color, "tiny")}</span>
        <strong>Nuevo bot</strong>
        <button id="open-connectors" class="topbar-link" type="button">${selectedConnectorIds.size} plugins instalados</button>
      </header>
      <div class="bot-builder-scroll">
        <form id="bot-form" class="bot-form">
          <div class="bot-preview">${renderMascot(botDraft.shape, botDraft.color, "large")}</div>
          <div class="palette" aria-label="Color del bot">
            ${BOT_COLORS.map((color) => `<button class="color-choice ${mascotColorClass(color)}${color === botDraft.color ? " selected" : ""}" type="button" data-bot-color="${color}" aria-label="Usar color ${color}" aria-pressed="${color === botDraft.color}"></button>`).join("")}
          </div>
          <div class="shape-picker" aria-label="Forma del bot">
            ${BOT_SHAPES.map((shape) => `<button class="shape-choice${shape === botDraft.shape ? " selected" : ""}" type="button" data-bot-shape="${shape}" aria-label="Usar forma ${shape}" aria-pressed="${shape === botDraft.shape}">${renderMascot(shape, "#2f91f5", "micro")}</button>`).join("")}
          </div>
          <label class="name-field"><span>Nombre</span><input id="bot-name" name="name" maxlength="60" placeholder="Nuevo bot" value="${escapeAttribute(botDraft.name)}" autocomplete="off" /></label>
          <button id="create-bot" class="create-bot-button" type="submit" ${botDraft.name.trim() ? "" : "disabled"}>Empezar</button>
          ${renderError()}
        </form>
        <section class="suggestion-section">
          <div class="suggestion-title"><strong>Sugerencias</strong><span>Empieza con una plantilla y personalízala</span></div>
          <div class="suggestion-grid">
            ${BOT_TEMPLATES.map((template) => `
              <button class="suggestion-card" type="button" data-template-id="${template.id}">
                ${renderMascot(template.shape, template.color, "medium")}
                <span><strong>${template.name}</strong><small>${template.description}</small></span>
                <i aria-hidden="true">→</i>
              </button>`).join("")}
          </div>
        </section>
      </div>
    </section>
  `, "new");
  bindSidebar();
  document.querySelector("#open-connectors")?.addEventListener("click", () => { activeView = "plugins"; render(); });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-bot-color]")) {
    button.addEventListener("click", () => {
      botDraft = { ...botDraft, color: button.dataset.botColor ?? BOT_COLORS[6] };
      renderBotBuilder();
    });
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-bot-shape]")) {
    button.addEventListener("click", () => {
      botDraft = { ...botDraft, shape: button.dataset.botShape ?? BOT_SHAPES[0] };
      renderBotBuilder();
    });
  }
  const nameInput = document.querySelector<HTMLInputElement>("#bot-name");
  nameInput?.addEventListener("input", () => {
    botDraft = { ...botDraft, name: nameInput.value };
    const createButton = document.querySelector<HTMLButtonElement>("#create-bot");
    if (createButton) createButton.disabled = !nameInput.value.trim();
  });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-template-id]")) {
    button.addEventListener("click", () => {
      const template = BOT_TEMPLATES.find((item) => item.id === button.dataset.templateId);
      if (!template) return;
      botDraft = { name: template.name, color: template.color, shape: template.shape };
      renderBotBuilder();
      document.querySelector<HTMLInputElement>("#bot-name")?.focus();
    });
  }
  document.querySelector("#bot-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    void createBot();
  });
}

async function createBot(): Promise<void> {
  setBusy(true);
  transientError = "";
  try {
    state = await desktopApi.createBot(botDraft);
    selectedConnectorIds = new Set(state.selectedConnectorIds);
    botDraft = { name: "", color: "#08be70", shape: "drop" };
    activeView = "bot-detail";
    settingsOpen = false;
    avatarEditorOpen = false;
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

async function createDefaultBot(): Promise<void> {
  if (!state.bots.length) {
    closeBotSettings();
    activeView = "bot-builder";
    render();
    return;
  }
  setBusy(true);
  transientError = "";
  try {
    state = await desktopApi.createBot({
      name: "Nuevo bot",
      color: BOT_COLORS[6],
      shape: BOT_SHAPES[0]
    });
    selectedConnectorIds = new Set(state.selectedConnectorIds);
    activeView = "bot-detail";
    closeBotSettings();
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function renderBotDetail(): void {
  const bot = state.bots.find((item) => item.id === state.activeBotId) ?? state.bots[0];
  if (!bot) {
    activeView = "bot-builder";
    renderBotBuilder();
    return;
  }
  if (bot.setup.step !== "complete") {
    renderBotOnboarding(bot);
    return;
  }
  renderReadyBot(bot);
}

function renderBotOnboarding(bot: BotProfile): void {
  appRoot.innerHTML = renderDesktopShell(`
    <section class="bot-chat-view">
      <header class="workspace-topbar">
        <button class="bot-avatar-trigger" type="button" data-open-settings aria-label="Personalizar ${escapeAttribute(bot.name)}">${renderBotAvatar(bot, "tiny")}</button>
        <strong>${escapeHtml(bot.name)}</strong>
        <div class="topbar-actions"><button id="edit-connectors" class="topbar-link" type="button">Plugins</button></div>
      </header>
      <div id="bot-setup-thread" class="bot-setup-thread">
        <div class="thread-date">Hoy · ${new Intl.DateTimeFormat("es-MX", { hour: "numeric", minute: "2-digit" }).format(new Date())}</div>
        <div class="assistant-bubble">Hola, soy ${escapeHtml(bot.name)}. Qué gusto conocerte.</div>
        ${renderSetupHistory(bot)}
        ${renderCurrentSetupStep(bot)}
        ${renderError()}
      </div>
      ${renderMessageComposer(bot.name)}
    </section>
  `, bot.id, settingsOpen ? bot : undefined);
  bindSidebar();
  bindBotSetup(bot);
  document.querySelector("#edit-connectors")?.addEventListener("click", () => { closeBotSettings(); activeView = "plugins"; render(); });
  requestAnimationFrame(() => {
    const thread = document.querySelector<HTMLElement>("#bot-setup-thread");
    if (thread) thread.scrollTop = thread.scrollHeight;
  });
}

function renderSetupHistory(bot: BotProfile): string {
  const setup = bot.setup;
  const chunks: string[] = [];
  if (setup.purpose) {
    chunks.push(renderAnsweredQuestion(
      "¿Para qué quieres usarme principalmente?",
      setupLabel("purpose", setup.purpose, setup.customAnswers.purpose)
    ));
    chunks.push(`<div class="assistant-bubble">${setup.purpose === "work" ? "Entendido: seré tu compañero de trabajo." : setup.purpose === "coding" ? "Perfecto: modo tecnología activado." : "Perfecto, ajustaré mi forma de trabajar a eso."}</div>`);
  }
  if (setup.workspace) {
    chunks.push(renderAnsweredQuestion(
      "¿Dónde vive tu trabajo en el día a día?",
      setupLabel("workspace", setup.workspace, setup.customAnswers.workspace)
    ));
    chunks.push(`<div class="assistant-bubble">${setup.workspace === "mix" ? "Perfecto, ese es todo el stack. Vamos a conectarlo." : "Listo, usaré esa herramienta como tu espacio principal."}</div>`);
  }
  if (setup.projectTool) {
    chunks.push(renderAnsweredQuestion(
      "¿Qué herramienta de proyectos debo usar?",
      setupLabel("project", setup.projectTool, setup.customAnswers.project)
    ));
    chunks.push('<div class="assistant-bubble">Ya tengo el contexto. Preparé tus conectores recomendados.</div>');
  }
  return chunks.join("");
}

function renderCurrentSetupStep(bot: BotProfile): string {
  if (bot.setup.step === "connections") return renderConnectionRecommendations(bot);
  const questions = {
    purpose: {
      title: "¿Para qué quieres usarme principalmente?",
      subtitle: "Ajustaré mi forma de trabajar a partir de tu respuesta.",
      placeholder: "Escribe tu propia respuesta"
    },
    workspace: {
      title: "¿Dónde vive tu trabajo en el día a día?",
      subtitle: "Puedo preparar estas herramientas para encontrarte información.",
      placeholder: "Escribe otra herramienta"
    },
    project: {
      title: "¿Qué herramienta de proyectos debo usar?",
      subtitle: "La añadiré junto con las demás herramientas que elegiste.",
      placeholder: "Escribe otra herramienta de proyectos"
    }
  } as const;
  const step = bot.setup.step as keyof typeof questions;
  const question = questions[step];
  const options = BOT_SETUP_OPTIONS[step];
  return `
    <section class="setup-question-card">
      <button class="question-close" type="button" aria-label="Omitir pregunta" data-skip-setup>×</button>
      <h2>${question.title}</h2>
      <p>${question.subtitle}</p>
      <div class="setup-options">
        ${options.map((option, index) => `<button type="button" data-setup-answer="${option.id}"><kbd>${String.fromCharCode(65 + index)}</kbd><span>${option.label}</span></button>`).join("")}
      </div>
      <form id="custom-setup-answer" class="custom-answer-form">
        <input name="customAnswer" maxlength="300" placeholder="${question.placeholder}" autocomplete="off" />
        <button type="submit">Enviar</button>
      </form>
    </section>`;
}

function renderAnsweredQuestion(title: string, answer: string): string {
  return `
    <section class="setup-question-card answered">
      <h2>${title}</h2>
      <div class="answered-row"><kbd>✓</kbd><span>${escapeHtml(answer)}</span><i>✓</i></div>
    </section>`;
}

function renderConnectionRecommendations(bot: BotProfile): string {
  const cards = connectionCards(bot);
  return `
    <div class="assistant-bubble">Voy a preparar estas herramientas. Autoriza tus cuentas y las guardaré cifradas solamente para tu usuario.</div>
    <section class="setup-question-card connection-setup-card">
      <h2>Conectores recomendados</h2>
      <p>Cada botón abre el inicio de sesión oficial del proveedor.</p>
      <div class="setup-connection-list">
        ${cards.length ? cards.map((connection) => `
          <article class="setup-connection-row">
            ${renderConnectorIcon(connection.icon, connection.name)}
            <span><strong>${connection.name}</strong><small>${connection.description}</small></span>
            ${renderConnectorAuthAction(connection.connectorId)}
          </article>`).join("") : '<div class="no-recommendations">No seleccionaste conectores. Puedes agregarlos después.</div>'}
      </div>
      <div class="oauth-truth-note"><strong>${connections.account.connected ? "Sesión personal activa" : "Primero inicia sesión"}</strong><span>${connections.account.connected ? escapeHtml(connections.account.email) : "Al conectar una herramienta abriremos el acceso de Agent Genia y después el del proveedor."}</span></div>
      <button id="finish-bot-setup" class="primary-action compact" type="button">Continuar con el bot</button>
    </section>`;
}

function connectionCards(bot: BotProfile): Array<{ connectorId: string; name: string; icon: string; description: string }> {
  const cards: Array<{ connectorId: string; name: string; icon: string; description: string }> = [];
  for (const connector of CONNECTOR_CATALOG.filter((item) => bot.connectorIds.includes(item.id))) {
    if (connector.id === "google-workspace") {
      cards.push(
        { connectorId: connector.id, name: "Gmail", icon: "google", description: "Correo, búsqueda y borradores" },
        { connectorId: connector.id, name: "Google Calendar", icon: "google", description: "Agenda, reuniones y eventos" }
      );
    } else cards.push({ connectorId: connector.id, name: connector.name, icon: connector.icon, description: connector.description });
  }
  return cards;
}

function setupLabel(group: keyof typeof BOT_SETUP_OPTIONS, value: string, custom?: string): string {
  return custom || BOT_SETUP_OPTIONS[group].find((option) => option.id === value)?.label || value;
}

function bindBotSetup(bot: BotProfile): void {
  bindConnectionActions();
  document.querySelector(".message-composer")?.addEventListener("submit", (event) => event.preventDefault());
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-setup-answer]")) {
    button.addEventListener("click", () => void answerCurrentSetup(bot, button.dataset.setupAnswer ?? ""));
  }
  document.querySelector("[data-skip-setup]")?.addEventListener("click", () => {
    const fallback = bot.setup.step === "purpose" ? "everything" : bot.setup.step === "workspace" ? "other" : "skip";
    void answerCurrentSetup(bot, fallback);
  });
  document.querySelector("#custom-setup-answer")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = (event.currentTarget as HTMLFormElement).elements.namedItem("customAnswer") as HTMLInputElement | null;
    const customText = input?.value.trim() ?? "";
    if (!customText) return input?.focus();
    const value = bot.setup.step === "purpose" ? "specific" : bot.setup.step === "workspace" ? "other" : "skip";
    void answerCurrentSetup(bot, value, customText);
  });
  document.querySelector("#finish-bot-setup")?.addEventListener("click", () => void answerCurrentSetup(bot, "complete"));
}

async function answerCurrentSetup(bot: BotProfile, value: string, customText?: string): Promise<void> {
  if (bot.setup.step === "complete") return;
  setBusy(true);
  transientError = "";
  try {
    const answer: BotSetupAnswer = { step: bot.setup.step, value, ...(customText ? { customText } : {}) };
    state = await desktopApi.answerBotSetup(bot.id, answer);
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function renderReadyBot(bot: BotProfile): void {
  const connectors = CONNECTOR_CATALOG.filter((connector) => bot.connectorIds.includes(connector.id));
  appRoot.innerHTML = renderDesktopShell(`
    <section class="bot-detail-view">
      <header class="workspace-topbar">
        <button class="bot-avatar-trigger" type="button" data-open-settings aria-label="Personalizar ${escapeAttribute(bot.name)}">${renderBotAvatar(bot, "tiny")}</button>
        <strong>${escapeHtml(bot.name)}</strong>
        <div class="topbar-actions"><button id="edit-connectors" class="topbar-link" type="button">Plugins</button></div>
      </header>
      <div class="bot-detail-content">
        <div class="detail-avatar">${renderBotAvatar(bot, "hero")}</div>
        <span class="ready-pill">BOT LISTO</span>
        <h1>${escapeHtml(bot.name)}</h1>
        <p>${connections.account.connected ? `Sesión activa como ${escapeHtml(connections.account.email)}. Cada conector usa únicamente la cuenta que tú autorizaste.` : "Inicia sesión al conectar una herramienta; cada proveedor abrirá su autorización oficial."}</p>
        <div class="selected-tools detailed">
          ${connectors.length ? connectors.map((connector) => `<article>${renderConnectorIcon(connector.icon, connector.name, true)}<strong>${connector.name}</strong>${renderConnectorAuthAction(connector.id)}</article>`).join("") : "<em>Sin conectores seleccionados</em>"}
        </div>
        <div class="detail-actions">
          <button id="new-bot-from-detail" class="primary-action compact" type="button">Crear otro bot</button>
          <button id="delete-bot" class="danger-action" type="button">Eliminar bot</button>
        </div>
      </div>
    </section>
  `, bot.id, settingsOpen ? bot : undefined);
  bindSidebar();
  bindConnectionActions();
  document.querySelector("#edit-connectors")?.addEventListener("click", () => { closeBotSettings(); activeView = "plugins"; render(); });
  document.querySelector("#new-bot-from-detail")?.addEventListener("click", () => void createDefaultBot());
  document.querySelector("#delete-bot")?.addEventListener("click", () => void deleteActiveBot(bot));
}

async function deleteActiveBot(bot: BotProfile): Promise<void> {
  if (!window.confirm(`¿Eliminar ${bot.name}?`)) return;
  setBusy(true);
  try {
    state = await desktopApi.deleteBot(bot.id);
    activeView = state.bots.length ? "bot-detail" : "bot-builder";
    closeBotSettings();
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function renderDesktopShell(content: string, activeId: string, settingsBot?: BotProfile): string {
  const normalizedSidebarQuery = sidebarQuery.trim().toLocaleLowerCase("es");
  const visibleBots = state.bots.filter((bot) => (
    !normalizedSidebarQuery
    || `${bot.name} ${bot.title} ${bot.description}`.toLocaleLowerCase("es").includes(normalizedSidebarQuery)
  ));
  return `
    <main class="desktop-shell${settingsBot ? " has-settings" : ""}">
      <aside class="desktop-sidebar">
        <div class="sidebar-window-bar">
          <div class="traffic-lights" aria-hidden="true"><i></i><i></i><i></i></div>
          <button class="sidebar-new-button" type="button" data-new-bot aria-label="Crear un bot">＋</button>
        </div>
        <label class="sidebar-search"><span aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"></circle><path d="m15.5 15.5 5 5"></path></svg></span><input id="sidebar-search" type="search" placeholder="Buscar" value="${escapeAttribute(sidebarQuery)}" autocomplete="off" /></label>
        <nav class="sidebar-nav" aria-label="Navegación de bots">
          ${visibleBots.map((bot) => `<button class="sidebar-row${activeId === bot.id ? " selected" : ""}" type="button" data-select-bot="${bot.id}">${renderBotAvatar(bot, "small")}<span><span class="sidebar-row-title"><strong>${escapeHtml(bot.name)}</strong><time>${formatBotTime(bot.createdAt)}</time></span><small>${escapeHtml(botSidebarPreview(bot))}</small></span></button>`).join("")}
        </nav>
        ${normalizedSidebarQuery && !visibleBots.length ? '<div class="sidebar-empty">No encontramos ese bot</div>' : ""}
        <div class="sidebar-footer">
          <button class="sidebar-footer-row" type="button" data-open-connectors><span class="sidebar-connector-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3v5M16 3v5M5 8h14v2a7 7 0 0 1-7 7 7 7 0 0 1-7-7V8Zm7 9v4"></path></svg></span><strong>Plugins</strong><small>${selectedConnectorIds.size ? `${selectedConnectorIds.size} instalados` : ""}</small></button>
          <button class="sidebar-user" type="button" data-open-billing><span>${escapeHtml((connections.account.name || connections.account.email || "A").slice(0, 1).toUpperCase())}</span><strong>${escapeHtml(connections.account.connected ? connections.account.name || connections.account.email : "Sin sesión")}</strong><small>${connections.account.connected ? "Plan y facturación" : "Iniciar sesión"}</small></button>
        </div>
      </aside>
      <div class="desktop-content">${content}</div>
      ${settingsBot ? renderBotSettings(settingsBot) : ""}
    </main>`;
}

function botSidebarPreview(bot: BotProfile): string {
  if (bot.title) return bot.title;
  if (bot.setup.step === "purpose") return "¿Con qué debería ayudarte más?";
  if (bot.setup.step === "workspace") return "¿Dónde vive tu trabajo?";
  if (bot.setup.step === "project") return "Elige tu herramienta de proyectos";
  if (bot.setup.step === "connections") return "Conecta tus herramientas";
  return bot.connectorIds.length ? `${bot.connectorIds.length} plugins conectados` : "Listo para trabajar";
}

function formatBotTime(createdAt: string): string {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("es-MX", { hour: "numeric", minute: "2-digit" }).format(date);
}

function renderMessageComposer(botName: string): string {
  return `
    <form class="message-composer" aria-label="Mensaje para ${escapeAttribute(botName)}">
      <button type="button" aria-label="Agregar">＋</button>
      <input type="text" placeholder="Mensaje para ${escapeAttribute(botName)}" aria-label="Mensaje" />
      <button class="composer-mic" type="button" aria-label="Mensaje de voz"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="3" width="8" height="12" rx="4"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3"></path></svg></button>
    </form>`;
}

function renderBotSettings(bot: BotProfile): string {
  return `
    <aside class="bot-settings-panel" aria-label="Configuración de ${escapeAttribute(bot.name)}">
      <header class="settings-header">
        <button type="button" data-close-settings aria-label="Volver">‹</button>
        <strong>Configuración</strong>
        <button type="button" data-close-settings aria-label="Cerrar">×</button>
      </header>
      <div class="settings-scroll">
        <button class="settings-avatar-button" type="button" data-toggle-avatar-editor aria-expanded="${avatarEditorOpen}">${renderBotAvatar(bot, "large")}<span>Editar avatar</span></button>
        <form id="bot-settings-form" class="bot-settings-form">
          <label><span>Nombre</span><input name="name" maxlength="60" value="${escapeAttribute(bot.name)}" /></label>
          <label><span>Título</span><input name="title" maxlength="100" placeholder="Describe qué hace tu agente" value="${escapeAttribute(bot.title)}" /></label>
          <label><span>Descripción</span><textarea name="description" maxlength="600" rows="5" placeholder="Para qué sirve este agente">${escapeHtml(bot.description)}</textarea></label>
          <label class="notification-setting">
            <span><strong>Notificaciones</strong><small>Recibe una alerta cuando este agente termine o necesite información</small></span>
            <input name="notificationsEnabled" type="checkbox" role="switch" ${bot.notificationsEnabled ? "checked" : ""} />
          </label>
        </form>
      </div>
      ${avatarEditorOpen ? renderAvatarEditor(bot) : ""}
    </aside>`;
}

function renderAvatarEditor(bot: BotProfile): string {
  return `
    <section class="avatar-editor-popover">
      <nav class="avatar-editor-tabs" aria-label="Tipo de avatar">
        ${(["bot", "generate", "upload"] as const).map((tab) => `<button type="button" data-avatar-tab="${tab}" class="${avatarEditorTab === tab ? "selected" : ""}">${tab === "bot" ? "Bot" : tab === "generate" ? "Generar" : "Subir"}</button>`).join("")}
      </nav>
      ${avatarEditorTab === "bot" ? `
        <div class="settings-shape-grid">
          ${BOT_SHAPES.map((shape) => `<button type="button" data-settings-shape="${shape}" class="${!bot.avatarDataUrl && bot.shape === shape ? "selected" : ""}">${renderMascot(shape, bot.color, "medium")}</button>`).join("")}
        </div>
        <div class="settings-color-grid">
          ${BOT_COLORS.map((color) => `<button type="button" data-settings-color="${color}" class="settings-color-choice ${mascotColorClass(color)}${!bot.avatarDataUrl && bot.color === color ? " selected" : ""}" aria-label="Usar color ${color}"></button>`).join("")}
        </div>` : avatarEditorTab === "generate" ? `
        <form id="generate-avatar-form" class="avatar-tool-panel">
          <strong>Generar una variante</strong>
          <p>Describe el carácter visual del bot y crearemos una combinación local de forma y color.</p>
          <input name="avatarPrompt" maxlength="160" placeholder="Ej. tranquilo, confiable y creativo" />
          <button type="submit">Generar</button>
        </form>` : `
        <div class="avatar-tool-panel">
          <strong>Subir una imagen</strong>
          <p>PNG, JPEG o WebP. Máximo 1 MB; se guarda solamente en este dispositivo.</p>
          <label class="upload-avatar-button">Elegir imagen<input id="avatar-upload" type="file" accept="image/png,image/jpeg,image/webp" /></label>
          ${bot.avatarDataUrl ? '<button id="remove-uploaded-avatar" class="remove-avatar-button" type="button">Volver al bot</button>' : ""}
        </div>`}
    </section>`;
}

function bindBotSettings(): void {
  if (!settingsOpen) return;
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-close-settings]")) {
    button.addEventListener("click", () => {
      settingsOpen = false;
      avatarEditorOpen = false;
      render();
    });
  }
  document.querySelector("[data-toggle-avatar-editor]")?.addEventListener("click", () => {
    avatarEditorOpen = !avatarEditorOpen;
    avatarEditorTab = "bot";
    render();
  });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-avatar-tab]")) {
    button.addEventListener("click", () => {
      avatarEditorTab = button.dataset.avatarTab as typeof avatarEditorTab;
      render();
    });
  }
  const form = document.querySelector<HTMLFormElement>("#bot-settings-form");
  const activeBot = state.bots.find((bot) => bot.id === state.activeBotId);
  if (!form || !activeBot) return;
  for (const field of form.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input[name='name'], input[name='title'], textarea[name='description']")) {
    field.addEventListener("input", () => {
      const patch: BotPatch = { [field.name]: field.value };
      if (field.name === "name" && !field.value.trim()) return;
      applyLocalBotPatch(activeBot.id, patch);
      scheduleBotSettingsSave(activeBot.id);
    });
    field.addEventListener("blur", () => {
      if (field.name === "name" && !field.value.trim()) render();
    });
  }
  const notification = form.elements.namedItem("notificationsEnabled") as HTMLInputElement | null;
  notification?.addEventListener("change", () => {
    applyLocalBotPatch(activeBot.id, { notificationsEnabled: notification.checked });
    scheduleBotSettingsSave(activeBot.id, 0);
  });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-settings-shape]")) {
    button.addEventListener("click", () => void saveAvatarPatch(activeBot.id, { shape: button.dataset.settingsShape, avatarDataUrl: "" }));
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-settings-color]")) {
    button.addEventListener("click", () => void saveAvatarPatch(activeBot.id, { color: button.dataset.settingsColor, avatarDataUrl: "" }));
  }
  document.querySelector("#generate-avatar-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const promptInput = (event.currentTarget as HTMLFormElement).elements.namedItem("avatarPrompt") as HTMLInputElement | null;
    const prompt = promptInput?.value.trim() ?? "";
    if (!prompt) return promptInput?.focus();
    const hash = [...prompt].reduce((total, character) => ((total * 31) + character.codePointAt(0)!) >>> 0, 17);
    void saveAvatarPatch(activeBot.id, {
      color: BOT_COLORS[hash % BOT_COLORS.length],
      shape: BOT_SHAPES[Math.floor(hash / BOT_COLORS.length) % BOT_SHAPES.length],
      avatarDataUrl: ""
    });
  });
  document.querySelector<HTMLInputElement>("#avatar-upload")?.addEventListener("change", (event) => void uploadAvatar(activeBot.id, event.currentTarget as HTMLInputElement));
  document.querySelector("#remove-uploaded-avatar")?.addEventListener("click", () => void saveAvatarPatch(activeBot.id, { avatarDataUrl: "" }));
}

function applyLocalBotPatch(botId: string, patch: BotPatch): void {
  state = { ...state, bots: state.bots.map((bot) => bot.id === botId ? updateBotProfile(bot, patch) : bot) };
}

function scheduleBotSettingsSave(botId: string, delay = 450): void {
  const previous = settingsSaveTimers.get(botId);
  if (previous !== undefined) window.clearTimeout(previous);
  const timer = window.setTimeout(() => void persistBotSettings(botId), delay);
  settingsSaveTimers.set(botId, timer);
}

async function persistBotSettings(botId: string): Promise<void> {
  settingsSaveTimers.delete(botId);
  const bot = state.bots.find((item) => item.id === botId);
  if (!bot) return;
  try {
    state = await desktopApi.updateBot(botId, {
      name: bot.name,
      title: bot.title,
      description: bot.description,
      notificationsEnabled: bot.notificationsEnabled
    });
  } catch (error) {
    transientError = errorMessage(error);
    render();
  }
}

async function saveAvatarPatch(botId: string, patch: BotPatch): Promise<void> {
  transientError = "";
  try {
    applyLocalBotPatch(botId, patch);
    state = await desktopApi.updateBot(botId, patch);
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

async function uploadAvatar(botId: string, input: HTMLInputElement): Promise<void> {
  const file = input.files?.[0];
  if (!file) return;
  if (!/^image\/(?:png|jpeg|webp)$/.test(file.type) || file.size > 1_000_000) {
    transientError = "Elige una imagen PNG, JPEG o WebP de máximo 1 MB.";
    render();
    return;
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result ?? "")), { once: true });
    reader.addEventListener("error", () => reject(reader.error), { once: true });
    reader.readAsDataURL(file);
  });
  await saveAvatarPatch(botId, { avatarDataUrl: dataUrl });
}

function bindSidebar(): void {
  document.querySelector("[data-new-bot]")?.addEventListener("click", () => void createDefaultBot());
  document.querySelector("[data-open-connectors]")?.addEventListener("click", () => { closeBotSettings(); activeView = "plugins"; render(); });
  document.querySelector("[data-open-billing]")?.addEventListener("click", () => void openBillingView());
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-select-bot]")) {
    button.addEventListener("click", () => void selectBot(button.dataset.selectBot ?? ""));
  }
  document.querySelector<HTMLInputElement>("#sidebar-search")?.addEventListener("input", (event) => {
    sidebarQuery = (event.currentTarget as HTMLInputElement).value;
    render();
    const input = document.querySelector<HTMLInputElement>("#sidebar-search");
    input?.focus();
    input?.setSelectionRange(sidebarQuery.length, sidebarQuery.length);
  });
  document.querySelector("[data-open-settings]")?.addEventListener("click", () => {
    settingsOpen = true;
    avatarEditorOpen = false;
    render();
  });
  bindBotSettings();
}

async function selectBot(botId: string): Promise<void> {
  try {
    state = await desktopApi.setActiveBot(botId);
    activeView = "bot-detail";
    closeBotSettings();
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function closeBotSettings(): void {
  settingsOpen = false;
  avatarEditorOpen = false;
}

function renderConnectorIcon(iconId: string, name: string, compact = false): string {
  const logoDataUrl = CONNECTOR_LOGO_DATA_URLS[iconId];
  if (logoDataUrl) {
    return `<span class="connector-icon${compact ? " compact" : ""}" aria-hidden="true"><img src="${logoDataUrl}" alt="" /></span>`;
  }
  const icon = iconCatalog[iconId];
  if (!icon) {
    const initials = name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2);
    return `<span class="connector-icon connector-icon-custom${compact ? " compact" : ""}" aria-hidden="true">${escapeHtml(initials)}</span>`;
  }
  const color = iconId === "github" || iconId === "notion" ? "18181b" : icon.hex;
  return `<span class="connector-icon${compact ? " compact" : ""}" aria-hidden="true"><svg viewBox="0 0 24 24" role="img"><path fill="#${color}" d="${icon.path}"></path></svg></span>`;
}

function renderMascot(shape: string, color: string, size: "micro" | "tiny" | "small" | "medium" | "large" | "hero"): string {
  const safeShape = BOT_SHAPES.includes(shape as (typeof BOT_SHAPES)[number]) ? shape : "circle";
  const safeColor = BOT_COLORS.includes(color as (typeof BOT_COLORS)[number]) || /^#[0-9a-f]{6}$/i.test(color) ? color : "#2f91f5";
  return `<span class="mascot mascot-${safeShape} mascot-${size} ${mascotColorClass(safeColor)}" aria-hidden="true"><i></i><i></i></span>`;
}

function renderBotAvatar(bot: BotProfile, size: "micro" | "tiny" | "small" | "medium" | "large" | "hero"): string {
  if (!bot.avatarDataUrl) return renderMascot(bot.shape, bot.color, size);
  return `<span class="uploaded-avatar uploaded-avatar-${size}" aria-hidden="true"><img src="${escapeAttribute(bot.avatarDataUrl)}" alt="" /></span>`;
}

function mascotColorClass(color: string): string {
  const classes: Record<string, string> = {
    "#a66d35": "mascot-color-brown",
    "#ff2f43": "mascot-color-red",
    "#ff6a00": "mascot-color-orange",
    "#ff9300": "mascot-color-amber",
    "#08be70": "mascot-color-green",
    "#11b9a9": "mascot-color-teal",
    "#18bda7": "mascot-color-mint",
    "#2f91f5": "mascot-color-blue",
    "#8654ed": "mascot-color-purple",
    "#f35ca7": "mascot-color-pink",
    "#808080": "mascot-color-gray"
  };
  return classes[color.toLocaleLowerCase()] ?? "mascot-color-blue";
}

function renderError(): string {
  return transientError ? `<p class="inline-error" role="alert">${escapeHtml(transientError)}</p>` : "";
}

function setBusy(busy: boolean): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>("button")) button.disabled = busy;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "No se pudo guardar el cambio.";
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[character] ?? character);
}

function escapeAttribute(value: string): string {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

function emptyConnectionSnapshot(): ConnectorConnectionSnapshot {
  const providers: Record<string, "google" | "microsoft" | "hubspot" | "salesforce" | undefined> = {
    "google-workspace": "google",
    "microsoft-365": "microsoft",
    hubspot: "hubspot",
    salesforce: "salesforce"
  };
  const managed = new Set<string>(HOSTED_CONNECTOR_IDS);
  return {
    account: { connected: false, required: true, email: "", name: "" },
    connectors: CONNECTOR_CATALOG.map((connector) => ({
      connectorId: connector.id,
      provider: managed.has(connector.id) ? "composio" : providers[connector.id] ?? null,
      available: managed.has(connector.id) || Boolean(providers[connector.id]),
      connected: false,
      account: "",
      reason: managed.has(connector.id) || providers[connector.id] ? "Listo para autorizar." : "OAuth no configurado."
    }))
  };
}

function emptyBillingSnapshot(): BillingSnapshot {
  return {
    configured: false,
    tier: "free",
    customer: false,
    subscription: null,
    plans: {
      basic: { name: "Plus", amount: 50, currency: "usd", interval: "month" },
      pro: { name: "Pro", amount: 200, currency: "usd", interval: "month" }
    }
  };
}

function createPreviewApi(): DesktopApi {
  let previewState = initialAppState();
  let previewConnections = emptyConnectionSnapshot();
  let previewBilling = { ...emptyBillingSnapshot(), configured: true };
  return {
    async bootstrap() { return structuredClone(previewState); },
    async connectionSnapshot() { return structuredClone(previewConnections); },
    async signIn() {
      previewConnections = { ...previewConnections, account: { connected: true, required: true, email: "demo@example.com", name: "Demo" } };
      return structuredClone(previewConnections);
    },
    async signOut() {
      previewConnections = emptyConnectionSnapshot();
      return structuredClone(previewConnections);
    },
    async connectConnector(connectorId) {
      previewConnections = {
        ...previewConnections,
        account: { connected: true, required: true, email: "demo@example.com", name: "Demo" },
        connectors: previewConnections.connectors.map((item) => item.connectorId === connectorId
          ? { ...item, connected: item.available, account: item.available ? "demo@example.com" : "" }
          : item)
      };
      return structuredClone(previewConnections);
    },
    async disconnectConnector(connectorId) {
      previewConnections = {
        ...previewConnections,
        connectors: previewConnections.connectors.map((item) => item.connectorId === connectorId
          ? { ...item, connected: false, account: "" }
          : item)
      };
      return structuredClone(previewConnections);
    },
    async billingSnapshot() { return structuredClone(previewBilling); },
    async startCheckout(tier) {
      previewBilling = { ...previewBilling, tier };
    },
    async openBillingPortal() {},
    async saveConnectors(connectorIds, onboardingCompleted) {
      previewState = {
        ...previewState,
        selectedConnectorIds: normalizeConnectorIds(connectorIds),
        onboardingCompleted: onboardingCompleted ?? previewState.onboardingCompleted
      };
      return structuredClone(previewState);
    },
    async createBot(draft) {
      const bot = createBotProfile(draft, previewState.selectedConnectorIds, crypto.randomUUID());
      previewState = { ...previewState, bots: [...previewState.bots, bot], activeBotId: bot.id, onboardingCompleted: true };
      return structuredClone(previewState);
    },
    async updateBot(botId, patch) {
      const bots = previewState.bots.map((bot) => bot.id === botId ? updateBotProfile(bot, patch) : bot);
      previewState = { ...previewState, bots, activeBotId: botId };
      return structuredClone(previewState);
    },
    async answerBotSetup(botId, answer) {
      const bots = previewState.bots.map((bot) => bot.id === botId ? applyBotSetupAnswer(bot, answer) : bot);
      previewState = { ...previewState, bots, activeBotId: botId };
      return structuredClone(previewState);
    },
    async setActiveBot(botId) {
      previewState = { ...previewState, activeBotId: previewState.bots.some((bot) => bot.id === botId) ? botId : null };
      return structuredClone(previewState);
    },
    async deleteBot(botId) {
      const bots = previewState.bots.filter((bot) => bot.id !== botId);
      previewState = { ...previewState, bots, activeBotId: previewState.activeBotId === botId ? bots[0]?.id ?? null : previewState.activeBotId };
      return structuredClone(previewState);
    }
  };
}
