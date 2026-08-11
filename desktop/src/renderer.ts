import {
  siCanva,
  siFigma,
  siGithub,
  siGoogle,
  siHubspot,
  siJira,
  siNotion,
  siSalesforce,
  siShopify,
  siSlack,
  siWoocommerce,
  siZoom,
  type SimpleIcon
} from "simple-icons";
import {
  BOT_COLORS,
  BOT_SETUP_OPTIONS,
  BOT_SHAPES,
  BOT_TEMPLATES,
  CONNECTOR_CATALOG,
  type AppState,
  type BotDraft,
  type BotProfile,
  type BotSetupAnswer,
  type DesktopApi,
  applyBotSetupAnswer,
  createBotProfile,
  initialAppState,
  normalizeConnectorIds
} from "./contracts";

declare global {
  interface Window {
    wrapperDesktop?: DesktopApi;
  }
}

type View = "connectors" | "bot-builder" | "bot-detail";

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
  shopify: siShopify,
  woocommerce: siWoocommerce
};

let state = initialAppState();
let activeView: View = "connectors";
let connectorQuery = "";
let selectedConnectorIds = new Set<string>();
let botDraft: BotDraft = { name: "", color: BOT_COLORS[6], shape: BOT_SHAPES[0] };
let transientError = "";

const desktopApi = window.wrapperDesktop ?? createPreviewApi();

void initialize();

async function initialize(): Promise<void> {
  try {
    state = await desktopApi.bootstrap();
    selectedConnectorIds = new Set(state.selectedConnectorIds);
    activeView = state.onboardingCompleted ? (state.bots.length ? "bot-detail" : "bot-builder") : "connectors";
    const preview = new URLSearchParams(window.location.search).get("preview");
    if (!window.wrapperDesktop && preview === "bot") {
      selectedConnectorIds = new Set(["google-workspace", "slack", "notion", "shopify"]);
      state = { ...state, onboardingCompleted: true, selectedConnectorIds: [...selectedConnectorIds] };
      activeView = "bot-builder";
    } else if (!window.wrapperDesktop && ["setup", "connections"].includes(preview ?? "")) {
      selectedConnectorIds = new Set(["google-workspace", "slack"]);
      let bot = createBotProfile({ name: "Juan", color: "#2f91f5", shape: "drop" }, [...selectedConnectorIds], "preview-bot");
      if (preview === "connections") {
        bot = applyBotSetupAnswer(bot, { step: "purpose", value: "work" });
        bot = applyBotSetupAnswer(bot, { step: "workspace", value: "mix" });
        bot = applyBotSetupAnswer(bot, { step: "project", value: "notion" });
      }
      state = { ...state, onboardingCompleted: true, selectedConnectorIds: [...selectedConnectorIds], bots: [bot], activeBotId: bot.id };
      activeView = "bot-detail";
    }
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function render(): void {
  if (activeView === "connectors") renderConnectors();
  else if (activeView === "bot-builder") renderBotBuilder();
  else renderBotDetail();
}

function renderConnectors(): void {
  const normalizedQuery = connectorQuery.trim().toLocaleLowerCase("es");
  const visible = CONNECTOR_CATALOG.filter((connector) => (
    !normalizedQuery
    || `${connector.name} ${connector.category} ${connector.description}`.toLocaleLowerCase("es").includes(normalizedQuery)
  ));
  const groups = ["Trabajo", "Ventas", "Desarrollo", "Diseño", "Comercio"];

  appRoot.innerHTML = `
    <main class="connector-screen">
      <div class="floating-mascot floating-mascot-left">${renderMascot("circle", "#18bda7", "small")}</div>
      <div class="floating-mascot floating-mascot-right">${renderMascot("circle", "#2f91f5", "small")}</div>
      <header class="connector-heading">
        <span class="eyebrow">CONECTA TU FLUJO</span>
        <h1>¿Qué usas todos los días?</h1>
        <p>Elige las herramientas que podrá usar cada bot. Puedes cambiar esta selección después.</p>
      </header>
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
                  return `
                    <button class="connector-card${selected ? " selected" : ""}" type="button" data-connector-id="${connector.id}" aria-pressed="${selected}">
                      ${renderConnectorIcon(connector.icon, connector.name)}
                      <span class="connector-card-copy"><strong>${connector.name}</strong><small>${connector.description}</small></span>
                      <span class="connector-check" aria-hidden="true">${selected ? "✓" : "+"}</span>
                    </button>`;
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
  document.querySelector("#connectors-next")?.addEventListener("click", () => void leaveConnectors("bot-builder"));
  document.querySelector("#connectors-back")?.addEventListener("click", () => void leaveConnectors(state.bots.length ? "bot-detail" : "bot-builder"));
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

function renderBotBuilder(): void {
  appRoot.innerHTML = renderDesktopShell(`
    <section class="bot-builder-view">
      <header class="workspace-topbar">
        <span>${renderMascot("circle", "#2f91f5", "tiny")}</span>
        <strong>Nuevo bot</strong>
        <button id="open-connectors" class="topbar-link" type="button">${selectedConnectorIds.size} conectores elegidos</button>
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
  document.querySelector("#open-connectors")?.addEventListener("click", () => { activeView = "connectors"; render(); });
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
    botDraft = { name: "", color: BOT_COLORS[6], shape: BOT_SHAPES[0] };
    activeView = "bot-detail";
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
        <span>${renderMascot(bot.shape, bot.color, "tiny")}</span>
        <strong>${escapeHtml(bot.name)}</strong>
        <button id="edit-connectors" class="topbar-link" type="button">Administrar conectores</button>
      </header>
      <div id="bot-setup-thread" class="bot-setup-thread">
        <div class="thread-date">Hoy · ${new Intl.DateTimeFormat("es-MX", { hour: "numeric", minute: "2-digit" }).format(new Date())}</div>
        <div class="assistant-bubble">Hola, soy ${escapeHtml(bot.name)}. Qué gusto conocerte.</div>
        ${renderSetupHistory(bot)}
        ${renderCurrentSetupStep(bot)}
        ${renderError()}
      </div>
    </section>
  `, bot.id);
  bindSidebar();
  bindBotSetup(bot);
  document.querySelector("#edit-connectors")?.addEventListener("click", () => { activeView = "connectors"; render(); });
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
  const connections = connectionCards(bot);
  return `
    <div class="assistant-bubble">Voy a preparar estas herramientas. Cada persona deberá autorizar su propia cuenta cuando el OAuth del proveedor esté configurado.</div>
    <section class="setup-question-card connection-setup-card">
      <h2>Conectores recomendados</h2>
      <p>Seleccionar una herramienta no concede acceso a ninguna cuenta.</p>
      <div class="setup-connection-list">
        ${connections.length ? connections.map((connection) => `
          <article class="setup-connection-row">
            ${renderConnectorIcon(connection.icon, connection.name)}
            <span><strong>${connection.name}</strong><small>${connection.description}</small></span>
            <span class="pending-auth">OAuth pendiente</span>
          </article>`).join("") : '<div class="no-recommendations">No seleccionaste conectores. Puedes agregarlos después.</div>'}
      </div>
      <div class="oauth-truth-note"><strong>Aún no inicia sesión</strong><span>Hace falta implementar el callback OAuth y guardar tokens separados por usuario para que estos accesos funcionen.</span></div>
      <button id="finish-bot-setup" class="primary-action compact" type="button">Continuar con el bot</button>
    </section>`;
}

function connectionCards(bot: BotProfile): Array<{ name: string; icon: string; description: string }> {
  const cards: Array<{ name: string; icon: string; description: string }> = [];
  for (const connector of CONNECTOR_CATALOG.filter((item) => bot.connectorIds.includes(item.id))) {
    if (connector.id === "google-workspace") {
      cards.push(
        { name: "Gmail", icon: "google", description: "Correo, búsqueda y borradores" },
        { name: "Google Calendar", icon: "google", description: "Agenda, reuniones y eventos" }
      );
    } else cards.push({ name: connector.name, icon: connector.icon, description: connector.description });
  }
  return cards;
}

function setupLabel(group: keyof typeof BOT_SETUP_OPTIONS, value: string, custom?: string): string {
  return custom || BOT_SETUP_OPTIONS[group].find((option) => option.id === value)?.label || value;
}

function bindBotSetup(bot: BotProfile): void {
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
        <span>${renderMascot(bot.shape, bot.color, "tiny")}</span>
        <strong>${escapeHtml(bot.name)}</strong>
        <button id="edit-connectors" class="topbar-link" type="button">Administrar conectores</button>
      </header>
      <div class="bot-detail-content">
        <div class="detail-avatar">${renderMascot(bot.shape, bot.color, "hero")}</div>
        <span class="ready-pill">BOT LISTO</span>
        <h1>${escapeHtml(bot.name)}</h1>
        <p>El perfil del bot quedó configurado. Los conectores aparecen como seleccionados; las cuentas siguen pendientes hasta implementar OAuth por usuario.</p>
        <div class="selected-tools">
          ${connectors.length ? connectors.map((connector) => `<span>${renderConnectorIcon(connector.icon, connector.name, true)}${connector.name}</span>`).join("") : "<em>Sin conectores seleccionados</em>"}
        </div>
        <div class="detail-actions">
          <button id="new-bot-from-detail" class="primary-action compact" type="button">Crear otro bot</button>
          <button id="delete-bot" class="danger-action" type="button">Eliminar bot</button>
        </div>
      </div>
    </section>
  `, bot.id);
  bindSidebar();
  document.querySelector("#edit-connectors")?.addEventListener("click", () => { activeView = "connectors"; render(); });
  document.querySelector("#new-bot-from-detail")?.addEventListener("click", () => { activeView = "bot-builder"; render(); });
  document.querySelector("#delete-bot")?.addEventListener("click", () => void deleteActiveBot(bot));
}

async function deleteActiveBot(bot: BotProfile): Promise<void> {
  if (!window.confirm(`¿Eliminar ${bot.name}?`)) return;
  setBusy(true);
  try {
    state = await desktopApi.deleteBot(bot.id);
    activeView = state.bots.length ? "bot-detail" : "bot-builder";
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function renderDesktopShell(content: string, activeId: string): string {
  return `
    <main class="desktop-shell">
      <aside class="desktop-sidebar">
        <div class="traffic-lights" aria-hidden="true"><i></i><i></i><i></i></div>
        <div class="sidebar-brand">${renderMascot("circle", "#2f91f5", "tiny")}<strong>Agent Genia</strong></div>
        <nav class="sidebar-nav" aria-label="Navegación de bots">
          <button class="sidebar-row${activeId === "new" ? " selected" : ""}" type="button" data-new-bot>${renderMascot("circle", "#2f91f5", "small")}<span><strong>Crear un bot</strong><small>Personaliza un nuevo agente</small></span></button>
          <button class="sidebar-row connector-row" type="button" data-open-connectors><span class="sidebar-connector-icon">⌘</span><span><strong>Conectores</strong><small>${selectedConnectorIds.size} herramientas elegidas</small></span></button>
          ${state.bots.length ? '<div class="sidebar-label">TUS BOTS</div>' : ""}
          ${state.bots.map((bot) => `<button class="sidebar-row${activeId === bot.id ? " selected" : ""}" type="button" data-select-bot="${bot.id}">${renderMascot(bot.shape, bot.color, "small")}<span><strong>${escapeHtml(bot.name)}</strong><small>${bot.setup.step === "complete" ? `${bot.connectorIds.length} conectores` : "Configurando perfil…"}</small></span></button>`).join("")}
        </nav>
        ${state.bots.length ? "" : '<div class="sidebar-empty">Todavía no hay bots</div>'}
        <div class="sidebar-user"><span>A</span><strong>Alan</strong></div>
      </aside>
      <div class="desktop-content">${content}</div>
    </main>`;
}

function bindSidebar(): void {
  document.querySelector("[data-new-bot]")?.addEventListener("click", () => { activeView = "bot-builder"; render(); });
  document.querySelector("[data-open-connectors]")?.addEventListener("click", () => { activeView = "connectors"; render(); });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-select-bot]")) {
    button.addEventListener("click", () => void selectBot(button.dataset.selectBot ?? ""));
  }
}

async function selectBot(botId: string): Promise<void> {
  try {
    state = await desktopApi.setActiveBot(botId);
    activeView = "bot-detail";
  } catch (error) {
    transientError = errorMessage(error);
  }
  render();
}

function renderConnectorIcon(iconId: string, name: string, compact = false): string {
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

function createPreviewApi(): DesktopApi {
  let previewState = initialAppState();
  return {
    async bootstrap() { return structuredClone(previewState); },
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
