import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { app, BrowserWindow, ipcMain, shell } from "electron";
import {
  type AppState,
  type BotDraft,
  type BotSetupAnswer,
  applyBotSetupAnswer,
  createBotProfile,
  initialAppState,
  normalizeAppState,
  normalizeConnectorIds
} from "./contracts";

const CHANNELS = Object.freeze({
  bootstrap: "desktop:bootstrap",
  saveConnectors: "desktop:save-connectors",
  createBot: "desktop:create-bot",
  answerBotSetup: "desktop:answer-bot-setup",
  setActiveBot: "desktop:set-active-bot",
  deleteBot: "desktop:delete-bot"
});

class DesktopStateStore {
  private state: AppState = initialAppState();
  private loaded = false;
  private writes: Promise<void> = Promise.resolve();

  constructor(private readonly filePath: string) {}

  async snapshot(): Promise<AppState> {
    await this.load();
    return structuredClone(this.state);
  }

  async update(mutator: (current: AppState) => AppState): Promise<AppState> {
    await this.load();
    this.state = normalizeAppState(mutator(structuredClone(this.state)));
    const snapshot = structuredClone(this.state);
    this.writes = this.writes.then(() => this.persist(snapshot));
    await this.writes;
    return structuredClone(snapshot);
  }

  private async load(): Promise<void> {
    if (this.loaded) return;
    this.loaded = true;
    try {
      this.state = normalizeAppState(JSON.parse(await readFile(this.filePath, "utf8")));
    } catch {
      this.state = initialAppState();
    }
  }

  private async persist(snapshot: AppState): Promise<void> {
    await mkdir(path.dirname(this.filePath), { recursive: true });
    const temporaryPath = `${this.filePath}.${process.pid}.tmp`;
    await writeFile(temporaryPath, `${JSON.stringify(snapshot, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
    await rename(temporaryPath, this.filePath);
  }
}

app.setName("Agent Genia");

let mainWindow: BrowserWindow | null = null;
let stateStore: DesktopStateStore;
const smokeTest = process.argv.includes("--smoke-test");

function registerDesktopIpc(): void {
  ipcMain.handle(CHANNELS.bootstrap, () => stateStore.snapshot());
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
  ipcMain.handle(CHANNELS.answerBotSetup, (_event, botId: unknown, answer: BotSetupAnswer) => stateStore.update((state) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    const index = state.bots.findIndex((bot) => bot.id === botId);
    if (index < 0) throw new Error("No encontramos ese bot.");
    const bots = [...state.bots];
    bots[index] = applyBotSetupAnswer(bots[index], answer);
    return { ...state, bots, activeBotId: botId };
  }));
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

app.whenReady().then(() => {
  stateStore = new DesktopStateStore(path.join(app.getPath("userData"), "desktop-state.json"));
  registerDesktopIpc();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
