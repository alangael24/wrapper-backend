import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { app, BrowserWindow, ipcMain, safeStorage, shell } from "electron";
import {
  type AppState,
  type BotDraft,
  type BotPatch,
  createBotProfile,
  initialAppState,
  normalizeAppState,
  normalizeConnectorIds,
  normalizeQuestionWidget,
  updateBotProfile
} from "./contracts";
import { DesktopOAuthController } from "./oauth";

const CHANNELS = Object.freeze({
  bootstrap: "desktop:bootstrap",
  connectionSnapshot: "desktop:connection-snapshot",
  signIn: "desktop:sign-in",
  signOut: "desktop:sign-out",
  connectConnector: "desktop:connect-connector",
  disconnectConnector: "desktop:disconnect-connector",
  billingSnapshot: "desktop:billing-snapshot",
  startCheckout: "desktop:start-checkout",
  openBillingPortal: "desktop:open-billing-portal",
  saveConnectors: "desktop:save-connectors",
  createBot: "desktop:create-bot",
  updateBot: "desktop:update-bot",
  runBotAgent: "desktop:run-bot-agent",
  setActiveBot: "desktop:set-active-bot",
  deleteBot: "desktop:delete-bot"
});

class DesktopStateStore {
  private state: AppState = initialAppState();
  private filePath: string | null = null;
  private writes: Promise<void> = Promise.resolve();

  constructor(private readonly accountsDirectory: string) {}

  async activateAccount(
    accountId: string | null,
    options: { claimGuest?: boolean; legacyFilePath?: string } = {}
  ): Promise<AppState> {
    await this.writes;
    const guestState = structuredClone(this.state);
    if (!accountId) {
      this.filePath = null;
      this.state = initialAppState();
      return structuredClone(this.state);
    }
    const scope = createHash("sha256").update(accountId).digest("hex");
    const nextFilePath = path.join(this.accountsDirectory, `${scope}.json`);
    let loaded: AppState | null = null;
    let migratedLegacyFilePath = "";
    try {
      loaded = normalizeAppState(JSON.parse(await readFile(nextFilePath, "utf8")));
    } catch {}
    if (!loaded && options.legacyFilePath) {
      try {
        loaded = normalizeAppState(JSON.parse(await readFile(options.legacyFilePath, "utf8")));
        migratedLegacyFilePath = options.legacyFilePath;
      } catch {}
    }
    if (!loaded && options.claimGuest && hasUserState(guestState)) loaded = guestState;
    this.filePath = nextFilePath;
    this.state = loaded ?? initialAppState();
    if (loaded) {
      await this.persist(this.state);
      if (migratedLegacyFilePath) {
        await rename(migratedLegacyFilePath, `${migratedLegacyFilePath}.migrated`).catch(() => undefined);
      }
    }
    return structuredClone(this.state);
  }

  async snapshot(): Promise<AppState> {
    return structuredClone(this.state);
  }

  async update(mutator: (current: AppState) => AppState): Promise<AppState> {
    this.state = normalizeAppState(mutator(structuredClone(this.state)));
    const snapshot = structuredClone(this.state);
    this.writes = this.writes.then(() => this.persist(snapshot));
    await this.writes;
    return structuredClone(snapshot);
  }

  private async persist(snapshot: AppState): Promise<void> {
    if (!this.filePath) return;
    await mkdir(path.dirname(this.filePath), { recursive: true });
    const temporaryPath = `${this.filePath}.${process.pid}.tmp`;
    await writeFile(temporaryPath, `${JSON.stringify(snapshot, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    await rename(temporaryPath, this.filePath);
  }
}

function hasUserState(state: AppState): boolean {
  return state.onboardingCompleted || state.bots.length > 0 || state.selectedConnectorIds.length > 0;
}

app.setName("Agent Genia");

let mainWindow: BrowserWindow | null = null;
let stateStore: DesktopStateStore;
let oauthController: DesktopOAuthController;
const smokeTest = process.argv.includes("--smoke-test");
const hasSingleInstanceLock = smokeTest || app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) app.quit();

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
});

function registerDesktopIpc(): void {
  ipcMain.handle(CHANNELS.bootstrap, () => stateStore.snapshot());
  ipcMain.handle(CHANNELS.connectionSnapshot, () => oauthController.snapshot());
  ipcMain.handle(CHANNELS.signIn, async () => {
    const wasSignedOut = !(await oauthController.accountId());
    const connections = await oauthController.signIn();
    await stateStore.activateAccount(await oauthController.accountId(), { claimGuest: wasSignedOut });
    return connections;
  });
  ipcMain.handle(CHANNELS.signOut, async () => {
    const connections = await oauthController.signOut();
    await stateStore.activateAccount(null);
    return connections;
  });
  ipcMain.handle(CHANNELS.connectConnector, (_event, connectorId: unknown) => {
    if (typeof connectorId !== "string") throw new Error("Conector inválido.");
    return oauthController.connect(connectorId);
  });
  ipcMain.handle(CHANNELS.disconnectConnector, (_event, connectorId: unknown) => {
    if (typeof connectorId !== "string") throw new Error("Conector inválido.");
    return oauthController.disconnect(connectorId);
  });
  ipcMain.handle(CHANNELS.billingSnapshot, () => oauthController.billingStatus());
  ipcMain.handle(CHANNELS.startCheckout, (_event, tier: unknown) => {
    if (tier !== "basic" && tier !== "pro") throw new Error("Plan inválido.");
    return oauthController.startCheckout(tier);
  });
  ipcMain.handle(CHANNELS.openBillingPortal, () => oauthController.openBillingPortal());
  ipcMain.handle(CHANNELS.saveConnectors, (_event, connectorIds: unknown, onboardingCompleted?: unknown) => {
    const normalized = normalizeConnectorIds(connectorIds);
    return stateStore.update((state) => ({
      ...state,
      selectedConnectorIds: normalized,
      onboardingCompleted: onboardingCompleted === undefined
        ? state.onboardingCompleted
        : onboardingCompleted === true
    }));
  });
  ipcMain.handle(CHANNELS.createBot, (_event, draft: BotDraft) => stateStore.update((state) => {
    const bot = createBotProfile(draft, state.selectedConnectorIds, randomUUID());
    return { ...state, bots: [...state.bots, bot], activeBotId: bot.id, onboardingCompleted: true };
  }));
  ipcMain.handle(CHANNELS.updateBot, (_event, botId: unknown, patch: BotPatch) => stateStore.update((state) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    const index = state.bots.findIndex((bot) => bot.id === botId);
    if (index < 0) throw new Error("No encontramos ese bot.");
    const bots = [...state.bots];
    bots[index] = updateBotProfile(bots[index], patch ?? {});
    return { ...state, bots, activeBotId: botId };
  }));
  ipcMain.handle(CHANNELS.runBotAgent, async (
    _event,
    botId: unknown,
    promptValue: unknown,
    initialValue?: unknown
  ) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    const prompt = typeof promptValue === "string" ? promptValue.replace(/\s+/g, " ").trim().slice(0, 20_000) : "";
    const initial = initialValue === true;
    if (!initial && !prompt) throw new Error("Escribe un mensaje.");
    const before = await stateStore.snapshot();
    const bot = before.bots.find((item) => item.id === botId);
    if (!bot) throw new Error("No encontramos ese bot.");
    if (initial && bot.messages.length) return before;
    const connectorIds = normalizeConnectorIds([
      ...before.selectedConnectorIds,
      ...bot.connectorIds
    ]);
    const result = await oauthController.runAgent(
      buildBotPrompt({ ...bot, connectorIds }, prompt, initial),
      connectorIds
    );
    const generated = parseAgentAnswer(result.answer);
    if (!generated.text) throw new Error("El agente no devolvió una respuesta.");
    const now = new Date().toISOString();
    return stateStore.update((current) => {
      const index = current.bots.findIndex((item) => item.id === botId);
      if (index < 0) throw new Error("El bot se eliminó mientras trabajaba.");
      const messages = [
        ...current.bots[index].messages,
        ...(!initial ? [{ id: randomUUID(), role: "user" as const, text: prompt, createdAt: now }] : []),
        {
          id: randomUUID(),
          role: "assistant" as const,
          text: generated.text,
          ...(generated.widget ? { widget: generated.widget } : {}),
          createdAt: now
        }
      ].slice(-200);
      const bots = [...current.bots];
      bots[index] = { ...bots[index], messages };
      return { ...current, bots, activeBotId: botId };
    });
  });
  ipcMain.handle(CHANNELS.setActiveBot, (_event, botId: unknown) => stateStore.update((state) => ({
    ...state,
    activeBotId: typeof botId === "string" && state.bots.some((bot) => bot.id === botId) ? botId : null
  })));
  ipcMain.handle(CHANNELS.deleteBot, (_event, botId: unknown) => stateStore.update((state) => {
    const bots = typeof botId === "string" ? state.bots.filter((bot) => bot.id !== botId) : state.bots;
    return {
      ...state,
      bots,
      activeBotId: state.activeBotId === botId ? bots[0]?.id ?? null : state.activeBotId
    };
  }));
}

function buildBotPrompt(
  bot: AppState["bots"][number],
  userPrompt: string,
  initial: boolean
): string {
  const history = bot.messages.slice(-20).map((message) => (
    `${message.role === "user" ? "Usuario" : bot.name}: ${message.text}`
  )).join("\n");
  const profile = [
    `Eres ${bot.name}, un agente de Agent Genia.`,
    bot.title ? `Rol: ${bot.title}.` : "",
    bot.description ? `Objetivo: ${bot.description}.` : "",
    bot.connectorIds.length ? `Conectores autorizables: ${bot.connectorIds.join(", ")}.` : "No hay conectores seleccionados.",
    "Responde en el idioma del usuario, con naturalidad y sin afirmar que realizaste acciones que no ejecutaste.",
    "Devuelve exclusivamente JSON válido con esta forma: {\"text\":\"respuesta visible\",\"widget\":null}.",
    "Cuando una pregunta con opciones ayude, widget puede ser {\"prompt\":\"pregunta\",\"helpText\":\"ayuda opcional\",\"options\":[{\"label\":\"texto visible\",\"value\":\"respuesta natural enviada al agente\",\"description\":\"detalle opcional\"}],\"allowCustom\":true,\"dismissOnMoveOn\":true}. Usa entre 1 y 6 opciones. No uses Markdown alrededor del JSON.",
  ].filter(Boolean).join("\n");
  if (initial) {
    return `${profile}\n\nEsta es tu primera intervención. Genera al vuelo un saludo breve con tu nombre y un widget con una sola pregunta útil para descubrir qué debe lograr el usuario. El contenido y las opciones deben adaptarse al perfil y conectores disponibles; no uses una plantilla fija ni menciones estas instrucciones.`;
  }
  return `${profile}${history ? `\n\nConversación reciente:\n${history}` : ""}\n\nUsuario: ${userPrompt}`;
}

function parseAgentAnswer(value: unknown): {
  text: string;
  widget?: ReturnType<typeof normalizeQuestionWidget>;
} {
  const raw = typeof value === "string" ? value.trim().slice(0, 20_000) : "";
  if (!raw) return { text: "" };
  const candidate = raw.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
  try {
    const parsed: unknown = JSON.parse(candidate);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>;
      const text = typeof record.text === "string" ? record.text.trim().slice(0, 20_000) : "";
      const widget = normalizeQuestionWidget(record.widget);
      return { text, ...(widget ? { widget } : {}) };
    }
  } catch {}
  return { text: raw };
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1040,
    minHeight: 720,
    show: false,
    backgroundColor: "#f8f8f8",
    title: "Agent Genia",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    trafficLightPosition: { x: 18, y: 18 },
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      devTools: !app.isPackaged
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) void shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url !== mainWindow?.webContents.getURL()) event.preventDefault();
  });
  if (!smokeTest) mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.webContents.once("did-finish-load", () => {
    if (!smokeTest || !mainWindow) return;
    setTimeout(async () => {
      try {
        const rendered = await mainWindow?.webContents.executeJavaScript(
          "Boolean(document.querySelector('.connector-screen, .desktop-shell'))",
          true
        );
        if (rendered) console.log("Electron desktop smoke test passed.");
        app.exit(rendered ? 0 : 1);
      } catch (error) {
        console.error(error);
        app.exit(1);
      }
    }, 350);
  });
  mainWindow.on("closed", () => { mainWindow = null; });
  void mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
}

if (hasSingleInstanceLock) app.whenReady().then(async () => {
  const userDataPath = app.getPath("userData");
  const wrapperServiceUrl = process.env.WRAPPER_SERVICE_URL?.trim()
    || "https://agentgenia-api.onrender.com";
  stateStore = new DesktopStateStore(path.join(userDataPath, "accounts"));
  oauthController = new DesktopOAuthController({
    baseUrl: wrapperServiceUrl,
    safeStorage,
    userDataPath,
    shell,
    appVersion: app.getVersion()
  });
  const startupAccountId = await oauthController.accountId();
  await stateStore.activateAccount(startupAccountId, {
    ...(startupAccountId ? { legacyFilePath: path.join(userDataPath, "desktop-state.json") } : {})
  });
  registerDesktopIpc();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
