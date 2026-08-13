export const CONNECTOR_CATALOG = Object.freeze([
  connector("google-workspace", "Google Workspace", "google", "Trabajo", "Correo, Drive, Calendar, Contacts y Sheets", "wrapper"),
  connector("slack", "Slack", "slack", "Trabajo", "Canales, mensajes y coordinación de equipo", "wrapper"),
  connector("notion", "Notion", "notion", "Trabajo", "Páginas, bases de datos y conocimiento", "wrapper"),
  connector("salesforce", "Salesforce", "salesforce", "Ventas", "Cuentas, contactos y oportunidades", "wrapper"),
  connector("microsoft-365", "Microsoft 365", "microsoft", "Trabajo", "Outlook, OneDrive, Calendar y Teams", "wrapper"),
  connector("linkedin", "LinkedIn", "linkedin", "Ventas", "Contactos, perfiles y relaciones profesionales", "wrapper"),
  connector("zoom", "Zoom", "zoom", "Trabajo", "Reuniones y seguimiento de llamadas", "wrapper"),
  connector("github", "GitHub", "github", "Desarrollo", "Repositorios, issues y pull requests", "wrapper"),
  connector("jira", "Jira", "jira", "Desarrollo", "Proyectos, tickets y ciclos de trabajo", "wrapper"),
  connector("linear", "Linear", "linear", "Desarrollo", "Issues, proyectos y ciclos de producto", "wrapper"),
  connector("asana", "Asana", "asana", "Trabajo", "Proyectos, tareas y responsables", "wrapper"),
  connector("clickup", "ClickUp", "clickup", "Trabajo", "Tareas, documentos y seguimiento de proyectos", "wrapper"),
  connector("figma", "Figma", "figma", "Diseño", "Archivos, comentarios y entregables de diseño", "wrapper"),
  connector("hubspot", "HubSpot", "hubspot", "Ventas", "Contactos, empresas y oportunidades", "wrapper"),
  connector("canva", "Canva", "canva", "Diseño", "Diseños, plantillas y contenido de marca", "wrapper"),
  connector("trello", "Trello", "trello", "Trabajo", "Tableros, listas, tarjetas y responsables", "wrapper"),
  connector("monday-com", "monday.com", "monday", "Trabajo", "Tableros, proyectos y automatizaciones de trabajo", "wrapper"),
  connector("intercom", "Intercom", "intercom", "Soporte", "Conversaciones, usuarios y atención al cliente", "wrapper"),
  connector("zendesk", "Zendesk", "zendesk", "Soporte", "Tickets, usuarios y operaciones de soporte", "wrapper"),
  connector("box", "Box", "box", "Trabajo", "Archivos, carpetas y colaboración empresarial", "wrapper"),
  connector("dropbox", "Dropbox", "dropbox", "Trabajo", "Archivos, carpetas y contenido compartido", "wrapper"),
  connector("docusign", "DocuSign", "docusign", "Trabajo", "Sobres, firmas y seguimiento de documentos", "wrapper"),
  connector("calendly", "Calendly", "calendly", "Trabajo", "Tipos de evento, disponibilidad y reuniones", "wrapper"),
  connector("loom", "Loom", "loom", "Trabajo", "Videos, transcripciones y espacios de equipo", "wrapper"),
  connector("outreach", "Outreach", "outreach", "Ventas", "Prospectos, secuencias y actividades comerciales", "wrapper"),
  connector("salesloft", "Salesloft", "salesloft", "Ventas", "Cadencias, personas y actividades de ventas", "wrapper"),
  connector("apollo", "Apollo", "apollo", "Ventas", "Personas, empresas y enriquecimiento comercial", "wrapper"),
  connector("clay", "Clay", "clay", "Ventas", "Tablas, enriquecimiento y flujos de prospección", "wrapper"),
  connector("zoominfo", "ZoomInfo", "zoominfo", "Ventas", "Contactos, empresas e inteligencia comercial", "wrapper"),
  connector("nooks", "Nooks", "nooks", "Ventas", "Marcador, sesiones y productividad de ventas", "wrapper"),
  connector("stripe", "Stripe", "stripe", "Finanzas", "Clientes, pagos, facturas y suscripciones", "wrapper"),
  connector("quickbooks", "QuickBooks", "quickbooks", "Finanzas", "Contabilidad, facturas, gastos y clientes", "wrapper"),
  connector("netsuite", "NetSuite", "netsuite", "Finanzas", "ERP, finanzas, clientes y operaciones", "wrapper"),
  connector("ramp", "Ramp", "ramp", "Finanzas", "Tarjetas, gastos, reembolsos y proveedores", "wrapper"),
  connector("workday", "Workday", "workday", "RR. HH.", "Personas, puestos y operaciones de recursos humanos", "wrapper"),
  connector("rippling", "Rippling", "rippling", "RR. HH.", "Empleados, nómina, dispositivos y aplicaciones", "wrapper"),
  connector("ashby", "Ashby", "ashby", "RR. HH.", "Candidatos, vacantes y procesos de contratación", "wrapper"),
  connector("greenhouse", "Greenhouse", "greenhouse", "RR. HH.", "Candidatos, entrevistas y vacantes", "wrapper"),
  connector("vercel", "Vercel", "vercel", "Desarrollo", "Proyectos, deployments, dominios y logs", "wrapper"),
  connector("tableau", "Tableau", "tableau", "Datos", "Fuentes, workbooks y visualizaciones", "wrapper"),
  connector("hex", "Hex", "hex", "Datos", "Proyectos, notebooks y análisis colaborativo", "wrapper"),
  connector("amplitude", "Amplitude", "amplitude", "Datos", "Analítica de producto, eventos y cohortes", "wrapper"),
  connector("mixpanel", "Mixpanel", "mixpanel", "Datos", "Eventos, funnels, retención y perfiles", "wrapper"),
  connector("snowflake", "Snowflake", "snowflake", "Datos", "Warehouses, bases de datos y consultas", "wrapper"),
  connector("databricks", "Databricks", "databricks", "Datos", "Lakehouse, notebooks, jobs y consultas", "wrapper"),
  connector("mailchimp", "Mailchimp", "mailchimp", "Marketing", "Audiencias, campañas y automatizaciones", "wrapper"),
  connector("shopify", "Shopify", "shopify", "Comercio", "Catálogo, tienda y herramientas publicadas", "ecom"),
  connector("tiendanube", "Tiendanube", "tiendanube", "Comercio", "Catálogo y contexto de la tienda", "ecom"),
  connector("woocommerce", "WooCommerce", "woocommerce", "Comercio", "Productos y contexto de WordPress Commerce", "ecom")
]);

// Verified against Composio's managed-auth catalog. These connectors can use
// hosted per-user authorization without embedding provider secrets in Electron.
export const MANAGED_CONNECTOR_IDS = Object.freeze([
  "google-workspace", "slack", "notion", "linkedin", "zoom", "github", "jira", "linear",
  "asana", "clickup", "figma", "canva", "trello", "monday-com", "intercom", "zendesk",
  "box", "dropbox", "calendly", "stripe", "quickbooks", "greenhouse", "mailchimp", "shopify",
  "apollo", "ashby", "vercel", "hex", "amplitude", "mixpanel", "databricks",
  "microsoft-365", "hubspot"
]);

// Toolkits that require an Auth Config owned by Agent Genia. Composio still
// stores and refreshes the resulting per-user credentials.
export const DIRECT_CONNECTOR_IDS = Object.freeze([
  "salesforce", "docusign", "outreach", "clay", "zoominfo", "netsuite",
  "ramp", "workday", "tableau", "snowflake", "woocommerce"
]);

export const HOSTED_CONNECTOR_IDS = Object.freeze([
  ...MANAGED_CONNECTOR_IDS,
  ...DIRECT_CONNECTOR_IDS
]);

export const BOT_COLORS = Object.freeze([
  "#a66d35", "#ff2f43", "#ff6a00", "#ff9300", "#08be70",
  "#11b9a9", "#2f91f5", "#8654ed", "#f35ca7", "#808080"
]);

export const BOT_SHAPES = Object.freeze([
  "circle", "bean", "square", "capsule", "triangle", "hexagon", "cloud", "drop"
]);

export const BOT_TEMPLATES = Object.freeze([
  Object.freeze({
    id: "night-shift",
    name: "Turno nocturno",
    description: "Trabaja durante la noche y prepara tu resumen de la mañana",
    color: "#ff6a00",
    shape: "hexagon"
  }),
  Object.freeze({
    id: "inbox-triage",
    name: "Gestor de bandeja",
    description: "Ordena tu correo y prepara respuestas con tu estilo",
    color: "#f35ca7",
    shape: "cloud"
  }),
  Object.freeze({
    id: "chief-of-staff",
    name: "Jefe de operaciones",
    description: "Organiza prioridades, seguimientos y decisiones pendientes",
    color: "#ff2f43",
    shape: "square"
  })
]);

export type ConnectorDefinition = (typeof CONNECTOR_CATALOG)[number];
export type BotColor = (typeof BOT_COLORS)[number];
export type BotShape = (typeof BOT_SHAPES)[number];
export type OAuthProviderId = "google" | "microsoft" | "hubspot" | "salesforce" | "pipedrive" | "zoho" | "composio";

export interface AccountConnectionStatus {
  connected: boolean;
  required: boolean;
  email: string;
  name: string;
}

export interface ConnectorConnectionStatus {
  connectorId: string;
  provider: OAuthProviderId | null;
  available: boolean;
  connected: boolean;
  account: string;
  reason: string;
}

export interface ConnectorConnectionSnapshot {
  account: AccountConnectionStatus;
  connectors: ConnectorConnectionStatus[];
}

export interface BillingSubscriptionStatus {
  stripe_subscription_id: string;
  tier: "basic" | "pro" | "business";
  stripe_price_id: string;
  status: string;
  cancel_at_period_end: boolean;
  current_period_end: number | null;
}

export interface BillingSnapshot {
  configured: boolean;
  tier: "free" | "basic" | "pro" | "business";
  customer: boolean;
  subscription: BillingSubscriptionStatus | null;
  plans: {
    basic: { name: string; amount: number; currency: string; interval: string; monthly_credits: number; max_concurrent_runs: number };
    pro: { name: string; amount: number; currency: string; interval: string; monthly_credits: number; max_concurrent_runs: number };
    business: { name: string; amount: number; currency: string; interval: string; monthly_credits: number; max_concurrent_runs: number };
  };
}

export interface BotProfile {
  id: string;
  name: string;
  title: string;
  description: string;
  color: BotColor;
  shape: BotShape;
  avatarDataUrl: string;
  notificationsEnabled: boolean;
  connectorIds: string[];
  messages: BotMessage[];
  workflows: BotWorkflow[];
  createdAt: string;
}

export interface BotWorkflow {
  id: string;
  title: string;
  summary: string;
  steps: string[];
  recordingId: string;
  recordingMimeType: "video/webm" | "video/mp4" | "";
  createdAt: string;
  updatedAt: string;
  lastRunAt: string;
}

export interface BotWorkflowDraft {
  title: string;
  summary: string;
  steps: string[];
}

export type TeachEntryPoint = "top_bar" | "composer_menu" | "screen_hover";
export type TeachRecordingPhase = "idle" | "recording" | "processing";

export interface TeachRecordingStatus {
  phase: TeachRecordingPhase;
  botId: string;
  botName: string;
  entryPoint: TeachEntryPoint | "";
  startedAt: string;
}

export interface TeachCapture {
  durationMs: number;
  frames: string[];
  mimeType: "video/webm" | "video/mp4" | "";
  videoBytes: Uint8Array;
}

export type BotComputerState = "disabled" | "pulling" | "running" | "hibernated" | "off" | "error";

export interface BotComputerSnapshot {
  configured: boolean;
  bot_id: string;
  provider: string | null;
  state: BotComputerState;
  viewer_url: string;
  viewer_expires_at: number;
  reason: string;
}

export interface BotMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  widget?: BotQuestionWidget;
  createdAt: string;
}

export interface BotQuestionWidget {
  prompt: string;
  helpText: string;
  options: BotQuestionOption[];
  allowCustom: boolean;
  dismissOnMoveOn: boolean;
}

export interface BotQuestionOption {
  label: string;
  value: string;
  description: string;
}

export interface AppState {
  version: 1;
  onboardingCompleted: boolean;
  selectedConnectorIds: string[];
  bots: BotProfile[];
  activeBotId: string | null;
}

export interface BotDraft {
  name: string;
  color: string;
  shape: string;
}

export interface BotPatch {
  name?: string;
  title?: string;
  description?: string;
  color?: string;
  shape?: string;
  avatarDataUrl?: string;
  notificationsEnabled?: boolean;
}

export interface DesktopApi {
  bootstrap(): Promise<AppState>;
  connectionSnapshot(): Promise<ConnectorConnectionSnapshot>;
  signIn(): Promise<ConnectorConnectionSnapshot>;
  signOut(): Promise<ConnectorConnectionSnapshot>;
  deleteAccount(): Promise<ConnectorConnectionSnapshot>;
  connectConnector(connectorId: string): Promise<ConnectorConnectionSnapshot>;
  disconnectConnector(connectorId: string): Promise<ConnectorConnectionSnapshot>;
  billingSnapshot(): Promise<BillingSnapshot>;
  startCheckout(tier: "basic" | "pro" | "business"): Promise<void>;
  openBillingPortal(): Promise<void>;
  computerStatus(botId: string): Promise<BotComputerSnapshot>;
  ensureComputer(botId: string, botName: string): Promise<BotComputerSnapshot>;
  handBackComputer(botId: string): Promise<BotComputerSnapshot>;
  deleteComputer(botId: string): Promise<{ deleted: boolean }>;
  openComputerViewer(url: string): Promise<void>;
  saveConnectors(connectorIds: string[], onboardingCompleted?: boolean): Promise<AppState>;
  createBot(draft: BotDraft): Promise<AppState>;
  updateBot(botId: string, patch: BotPatch): Promise<AppState>;
  runBotAgent(botId: string, prompt: string, initial?: boolean): Promise<AppState>;
  getTeachRecordingStatus(): Promise<TeachRecordingStatus>;
  startTeachRecording(botId: string, entryPoint: TeachEntryPoint): Promise<TeachRecordingStatus>;
  stopTeachRecording(botId: string, capture: TeachCapture): Promise<AppState>;
  discardTeachRecording(botId: string): Promise<TeachRecordingStatus>;
  runBotWorkflow(botId: string, workflowId: string): Promise<AppState>;
  deleteBotWorkflow(botId: string, workflowId: string): Promise<AppState>;
  setActiveBot(botId: string | null): Promise<AppState>;
  deleteBot(botId: string): Promise<AppState>;
}

export function initialAppState(): AppState {
  return {
    version: 1,
    onboardingCompleted: false,
    selectedConnectorIds: [],
    bots: [],
    activeBotId: null
  };
}

export function normalizeAppState(value: unknown): AppState {
  const fallback = initialAppState();
  if (!isRecord(value)) return fallback;
  const validConnectorIds = new Set(CONNECTOR_CATALOG.map((item) => item.id));
  const selectedConnectorIds = uniqueStrings(value.selectedConnectorIds)
    .filter((id) => validConnectorIds.has(id));
  const bots = Array.isArray(value.bots)
    ? value.bots.slice(0, 100).map(normalizeBot).filter((bot): bot is BotProfile => Boolean(bot))
    : [];
  const activeBotId = typeof value.activeBotId === "string" && bots.some((bot) => bot.id === value.activeBotId)
    ? value.activeBotId
    : bots[0]?.id ?? null;
  return {
    version: 1,
    onboardingCompleted: value.onboardingCompleted === true,
    selectedConnectorIds,
    bots,
    activeBotId
  };
}

export function normalizeConnectorIds(value: unknown): string[] {
  const allowed = new Set(CONNECTOR_CATALOG.map((item) => item.id));
  return uniqueStrings(value).filter((id) => allowed.has(id));
}

export function createBotProfile(draft: BotDraft, connectorIds: string[], id: string, now = new Date()): BotProfile {
  const name = cleanBotName(draft.name);
  if (!name) throw new Error("Escribe un nombre para el bot.");
  const color = BOT_COLORS.includes(draft.color as BotColor) ? draft.color as BotColor : BOT_COLORS[6];
  const shape = BOT_SHAPES.includes(draft.shape as BotShape) ? draft.shape as BotShape : BOT_SHAPES[0];
  return {
    id,
    name,
    title: "",
    description: "",
    color,
    shape,
    avatarDataUrl: "",
    notificationsEnabled: true,
    connectorIds: normalizeConnectorIds(connectorIds),
    messages: [],
    workflows: [],
    createdAt: now.toISOString()
  };
}

export function updateBotProfile(bot: BotProfile, patch: BotPatch): BotProfile {
  const name = patch.name === undefined ? bot.name : cleanBotName(patch.name);
  if (!name) throw new Error("El bot necesita un nombre.");
  const color = patch.color === undefined
    ? bot.color
    : BOT_COLORS.includes(patch.color as BotColor) ? patch.color as BotColor : bot.color;
  const shape = patch.shape === undefined
    ? bot.shape
    : BOT_SHAPES.includes(patch.shape as BotShape) ? patch.shape as BotShape : bot.shape;
  const avatarDataUrl = patch.avatarDataUrl === undefined ? bot.avatarDataUrl : normalizeAvatarDataUrl(patch.avatarDataUrl);
  return {
    ...bot,
    name,
    title: patch.title === undefined ? bot.title : cleanProfileText(patch.title, 100),
    description: patch.description === undefined ? bot.description : cleanProfileText(patch.description, 600),
    color,
    shape,
    avatarDataUrl,
    notificationsEnabled: patch.notificationsEnabled === undefined ? bot.notificationsEnabled : patch.notificationsEnabled === true
  };
}

function connector(id: string, name: string, icon: string, category: string, description: string, source: "wrapper" | "ecom") {
  return Object.freeze({ id, name, icon, category, description, source });
}

function normalizeBot(value: unknown): BotProfile | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  try {
    const createdAt = typeof value.createdAt === "string" && !Number.isNaN(Date.parse(value.createdAt))
      ? new Date(value.createdAt)
      : new Date();
    const bot = createBotProfile({
      name: typeof value.name === "string" ? value.name : "",
      color: typeof value.color === "string" ? value.color : "",
      shape: typeof value.shape === "string" ? value.shape : ""
    }, normalizeConnectorIds(value.connectorIds), value.id.slice(0, 100), createdAt);
    return {
      ...bot,
      title: cleanProfileText(value.title, 100),
      description: cleanProfileText(value.description, 600),
      avatarDataUrl: safeAvatarDataUrl(value.avatarDataUrl),
      notificationsEnabled: value.notificationsEnabled !== false,
      messages: normalizeBotMessages(value.messages),
      workflows: normalizeBotWorkflows(value.workflows)
    };
  } catch {
    return null;
  }
}

export function createBotWorkflow(
  draft: BotWorkflowDraft,
  id: string,
  recordingId: string,
  recordingMimeType: BotWorkflow["recordingMimeType"],
  now = new Date()
): BotWorkflow {
  const title = cleanProfileText(draft.title, 120).replace(/\s+/g, " ");
  const steps = normalizeWorkflowSteps(draft.steps);
  if (!title || !steps.length) throw new Error("No pudimos extraer los pasos de la tarea.");
  const timestamp = now.toISOString();
  return {
    id: id.slice(0, 100),
    title,
    summary: cleanProfileText(draft.summary, 500),
    steps,
    recordingId: recordingId.slice(0, 100),
    recordingMimeType,
    createdAt: timestamp,
    updatedAt: timestamp,
    lastRunAt: ""
  };
}

function normalizeBotWorkflows(value: unknown): BotWorkflow[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-50).flatMap((item): BotWorkflow[] => {
    if (!isRecord(item)) return [];
    try {
      const createdAt = normalizeDate(item.createdAt);
      const workflow = createBotWorkflow({
        title: typeof item.title === "string" ? item.title : "",
        summary: typeof item.summary === "string" ? item.summary : "",
        steps: Array.isArray(item.steps) ? item.steps.filter((step): step is string => typeof step === "string") : []
      }, typeof item.id === "string" ? item.id : crypto.randomUUID(), typeof item.recordingId === "string" ? item.recordingId : "", normalizeRecordingMimeType(item.recordingMimeType), new Date(createdAt));
      return [{
        ...workflow,
        updatedAt: normalizeDate(item.updatedAt, createdAt),
        lastRunAt: item.lastRunAt ? normalizeDate(item.lastRunAt, "") : ""
      }];
    } catch {
      return [];
    }
  });
}

function normalizeWorkflowSteps(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 30).flatMap((step): string[] => {
    const normalized = cleanProfileText(step, 600);
    return normalized ? [normalized] : [];
  });
}

function normalizeRecordingMimeType(value: unknown): BotWorkflow["recordingMimeType"] {
  return value === "video/webm" || value === "video/mp4" ? value : "";
}

function normalizeDate(value: unknown, fallback = new Date().toISOString()): string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value))
    ? new Date(value).toISOString()
    : fallback;
}

function cleanBotName(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.replace(/\s+/g, " ").trim().slice(0, 60);
}

function cleanProfileText(value: unknown, limit: number): string {
  return typeof value === "string" ? value.replace(/\r\n?/g, "\n").trim().slice(0, limit) : "";
}

function normalizeBotMessages(value: unknown): BotMessage[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-200).flatMap((item): BotMessage[] => {
    if (!isRecord(item) || (item.role !== "user" && item.role !== "assistant")) return [];
    const text = cleanProfileText(item.text, 20_000);
    if (!text) return [];
    const createdAt = typeof item.createdAt === "string" && !Number.isNaN(Date.parse(item.createdAt))
      ? new Date(item.createdAt).toISOString()
      : new Date().toISOString();
    return [{
      id: typeof item.id === "string" && item.id ? item.id.slice(0, 100) : crypto.randomUUID(),
      role: item.role,
      text,
      ...(item.role === "assistant" && normalizeQuestionWidget(item.widget)
        ? { widget: normalizeQuestionWidget(item.widget)! }
        : {}),
      createdAt
    }];
  });
}

export function normalizeQuestionWidget(value: unknown): BotQuestionWidget | undefined {
  if (!isRecord(value)) return undefined;
  const prompt = cleanProfileText(value.prompt, 300);
  if (!prompt || !Array.isArray(value.options)) return undefined;
  const options = value.options.slice(0, 6).flatMap((item): BotQuestionOption[] => {
    if (!isRecord(item)) return [];
    const label = cleanProfileText(item.label, 120);
    if (!label) return [];
    return [{
      label,
      value: cleanProfileText(item.value, 300) || label,
      description: cleanProfileText(item.description, 240)
    }];
  });
  if (!options.length) return undefined;
  return {
    prompt,
    helpText: cleanProfileText(value.helpText, 300),
    options,
    allowCustom: value.allowCustom !== false,
    dismissOnMoveOn: value.dismissOnMoveOn !== false
  };
}

function normalizeAvatarDataUrl(value: unknown): string {
  if (value === "" || value === undefined || value === null) return "";
  if (typeof value !== "string" || value.length > 1_500_000) throw new Error("La imagen del avatar es demasiado grande.");
  if (!/^data:image\/(?:png|jpeg|webp);base64,[a-z0-9+/=]+$/i.test(value)) {
    throw new Error("El avatar debe ser PNG, JPEG o WebP.");
  }
  return value;
}

function safeAvatarDataUrl(value: unknown): string {
  try { return normalizeAvatarDataUrl(value); }
  catch { return ""; }
}

function uniqueStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((item): item is string => typeof item === "string"))];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
