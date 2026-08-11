export const CONNECTOR_CATALOG = Object.freeze([
  connector("google-workspace", "Google Workspace", "google", "Trabajo", "Correo, Drive, Calendar, Contacts y Sheets", "outcome"),
  connector("slack", "Slack", "slack", "Trabajo", "Canales, mensajes y coordinación de equipo", "outcome"),
  connector("notion", "Notion", "notion", "Trabajo", "Páginas, bases de datos y conocimiento", "outcome"),
  connector("salesforce", "Salesforce", "salesforce", "Ventas", "Cuentas, contactos y oportunidades", "outcome"),
  connector("microsoft-365", "Microsoft 365", "microsoft", "Trabajo", "Outlook, OneDrive, Calendar y Teams", "outcome"),
  connector("linkedin", "LinkedIn", "linkedin", "Ventas", "Contactos, perfiles y relaciones profesionales", "outcome"),
  connector("zoom", "Zoom", "zoom", "Trabajo", "Reuniones y seguimiento de llamadas", "outcome"),
  connector("github", "GitHub", "github", "Desarrollo", "Repositorios, issues y pull requests", "outcome"),
  connector("jira", "Jira", "jira", "Desarrollo", "Proyectos, tickets y ciclos de trabajo", "outcome"),
  connector("linear", "Linear", "linear", "Desarrollo", "Issues, proyectos y ciclos de producto", "outcome"),
  connector("asana", "Asana", "asana", "Trabajo", "Proyectos, tareas y responsables", "outcome"),
  connector("clickup", "ClickUp", "clickup", "Trabajo", "Tareas, documentos y seguimiento de proyectos", "outcome"),
  connector("figma", "Figma", "figma", "Diseño", "Archivos, comentarios y entregables de diseño", "outcome"),
  connector("hubspot", "HubSpot", "hubspot", "Ventas", "Contactos, empresas y oportunidades", "outcome"),
  connector("canva", "Canva", "canva", "Diseño", "Diseños, plantillas y contenido de marca", "outcome"),
  connector("shopify", "Shopify", "shopify", "Comercio", "Catálogo, tienda y herramientas publicadas", "ecom"),
  connector("tiendanube", "Tiendanube", "tiendanube", "Comercio", "Catálogo y contexto de la tienda", "ecom"),
  connector("woocommerce", "WooCommerce", "woocommerce", "Comercio", "Productos y contexto de WordPress Commerce", "ecom")
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

export const BOT_SETUP_OPTIONS = Object.freeze({
  purpose: Object.freeze([
    option("work", "Trabajo"),
    option("personal", "Vida personal"),
    option("coding", "Programación / tecnología"),
    option("everything", "Un poco de todo"),
    option("specific", "Algo específico")
  ]),
  workspace: Object.freeze([
    option("google", "Gmail + Google Calendar"),
    option("slack", "Slack"),
    option("projects", "Linear / Notion / Asana"),
    option("mix", "Una mezcla de esas"),
    option("other", "Otra herramienta")
  ]),
  project: Object.freeze([
    option("linear", "Linear"),
    option("notion", "Notion"),
    option("asana", "Asana"),
    option("clickup", "ClickUp"),
    option("skip", "Omitir proyectos por ahora")
  ])
});

export type ConnectorDefinition = (typeof CONNECTOR_CATALOG)[number];
export type BotColor = (typeof BOT_COLORS)[number];
export type BotShape = (typeof BOT_SHAPES)[number];
export type BotSetupStep = "purpose" | "workspace" | "project" | "connections" | "complete";

export interface BotSetupState {
  step: BotSetupStep;
  purpose: string;
  workspace: string;
  projectTool: string;
  customAnswers: Partial<Record<"purpose" | "workspace" | "project", string>>;
}

export interface BotSetupAnswer {
  step: "purpose" | "workspace" | "project" | "connections";
  value: string;
  customText?: string;
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
  setup: BotSetupState;
  createdAt: string;
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
  saveConnectors(connectorIds: string[], onboardingCompleted?: boolean): Promise<AppState>;
  createBot(draft: BotDraft): Promise<AppState>;
  updateBot(botId: string, patch: BotPatch): Promise<AppState>;
  answerBotSetup(botId: string, answer: BotSetupAnswer): Promise<AppState>;
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
    setup: initialBotSetup(),
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

export function initialBotSetup(): BotSetupState {
  return { step: "purpose", purpose: "", workspace: "", projectTool: "", customAnswers: {} };
}

export function applyBotSetupAnswer(bot: BotProfile, answer: BotSetupAnswer): BotProfile {
  if (bot.setup.step !== answer.step) throw new Error("Esta pregunta ya no está activa.");
  const customText = cleanAnswer(answer.customText);
  const connectorIds = new Set(bot.connectorIds);
  const setup: BotSetupState = structuredClone(bot.setup);

  if (answer.step === "purpose") {
    assertOption("purpose", answer.value);
    setup.purpose = answer.value;
    if (answer.value === "specific" && customText) setup.customAnswers.purpose = customText;
    if (answer.value === "coding") connectorIds.add("github");
    setup.step = ["work", "everything"].includes(answer.value) ? "workspace" : "connections";
  } else if (answer.step === "workspace") {
    assertOption("workspace", answer.value);
    setup.workspace = answer.value;
    if (answer.value === "other" && customText) setup.customAnswers.workspace = customText;
    if (["google", "mix"].includes(answer.value)) connectorIds.add("google-workspace");
    if (["slack", "mix"].includes(answer.value)) connectorIds.add("slack");
    setup.step = ["projects", "mix"].includes(answer.value) ? "project" : "connections";
  } else if (answer.step === "project") {
    assertOption("project", answer.value);
    setup.projectTool = answer.value;
    if (answer.value !== "skip") connectorIds.add(answer.value);
    if (customText) setup.customAnswers.project = customText;
    setup.step = "connections";
  } else {
    if (answer.value !== "complete") throw new Error("Respuesta de configuración inválida.");
    setup.step = "complete";
  }

  return { ...bot, setup, connectorIds: normalizeConnectorIds([...connectorIds]) };
}

function connector(id: string, name: string, icon: string, category: string, description: string, source: "outcome" | "ecom") {
  return Object.freeze({ id, name, icon, category, description, source });
}

function option(id: string, label: string) {
  return Object.freeze({ id, label });
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
      setup: normalizeBotSetup(value.setup)
    };
  } catch {
    return null;
  }
}

function normalizeBotSetup(value: unknown): BotSetupState {
  if (!isRecord(value)) return initialBotSetup();
  const steps: BotSetupStep[] = ["purpose", "workspace", "project", "connections", "complete"];
  const customAnswers = isRecord(value.customAnswers) ? value.customAnswers : {};
  return {
    step: steps.includes(value.step as BotSetupStep) ? value.step as BotSetupStep : "purpose",
    purpose: optionValue("purpose", value.purpose),
    workspace: optionValue("workspace", value.workspace),
    projectTool: optionValue("project", value.projectTool),
    customAnswers: {
      ...(cleanAnswer(customAnswers.purpose) ? { purpose: cleanAnswer(customAnswers.purpose) } : {}),
      ...(cleanAnswer(customAnswers.workspace) ? { workspace: cleanAnswer(customAnswers.workspace) } : {}),
      ...(cleanAnswer(customAnswers.project) ? { project: cleanAnswer(customAnswers.project) } : {})
    }
  };
}

function assertOption(group: keyof typeof BOT_SETUP_OPTIONS, value: string): void {
  if (!BOT_SETUP_OPTIONS[group].some((item) => item.id === value)) {
    throw new Error("Respuesta de configuración inválida.");
  }
}

function optionValue(group: keyof typeof BOT_SETUP_OPTIONS, value: unknown): string {
  return typeof value === "string" && BOT_SETUP_OPTIONS[group].some((item) => item.id === value) ? value : "";
}

function cleanAnswer(value: unknown): string {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, 300) : "";
}

function cleanBotName(value: unknown): string {
  if (typeof value !== "string") return "";
  return value.replace(/\s+/g, " ").trim().slice(0, 60);
}

function cleanProfileText(value: unknown, limit: number): string {
  return typeof value === "string" ? value.replace(/\r\n?/g, "\n").trim().slice(0, limit) : "";
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
