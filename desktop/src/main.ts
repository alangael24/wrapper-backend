import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, desktopCapturer, ipcMain, safeStorage, session, shell } from "electron";
import { autoUpdater } from "electron-updater";
import {
  type AppState,
  type BotDraft,
  type BotPatch,
  type BotWidgetAction,
  type BotWorkflow,
  type TeachCapture,
  type TeachEntryPoint,
  type TeachRecordingStatus,
  createBotWorkflow,
  createBotProfile,
  initialAppState,
  normalizeAppState,
  normalizeConnectorIds,
  normalizeQuestionWidget,
  updateBotProfile
} from "./contracts";
import { AccountStateConflictError, DesktopOAuthController, safeComputerViewerUrl } from "./oauth";
import type { LocalAgentRuntime } from "./local-agent-runtime";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const CHANNELS = Object.freeze({
  bootstrap: "desktop:bootstrap",
  refreshAccountState: "desktop:refresh-account-state",
  connectionSnapshot: "desktop:connection-snapshot",
  signIn: "desktop:sign-in",
  signOut: "desktop:sign-out",
  deleteAccount: "desktop:delete-account",
  connectConnector: "desktop:connect-connector",
  disconnectConnector: "desktop:disconnect-connector",
  billingSnapshot: "desktop:billing-snapshot",
  startCheckout: "desktop:start-checkout",
  openBillingPortal: "desktop:open-billing-portal",
  whatsappStatus: "desktop:whatsapp-status",
  startWhatsappLink: "desktop:start-whatsapp-link",
  unlinkWhatsapp: "desktop:unlink-whatsapp",
  computerStatus: "desktop:computer-status",
  ensureComputer: "desktop:ensure-computer",
  handBackComputer: "desktop:hand-back-computer",
  deleteComputer: "desktop:delete-computer",
  openComputerViewer: "desktop:open-computer-viewer",
  saveConnectors: "desktop:save-connectors",
  createBot: "desktop:create-bot",
  updateBot: "desktop:update-bot",
  warmBotAgent: "desktop:warm-bot-agent",
  runBotAgent: "desktop:run-bot-agent",
  agentDelta: "desktop:agent-delta",
  getTeachRecordingStatus: "desktop:get-teach-recording-status",
  startTeachRecording: "desktop:start-teach-recording",
  stopTeachRecording: "desktop:stop-teach-recording",
  discardTeachRecording: "desktop:discard-teach-recording",
  runBotWorkflow: "desktop:run-bot-workflow",
  deleteBotWorkflow: "desktop:delete-bot-workflow",
  setActiveBot: "desktop:set-active-bot",
  deleteBot: "desktop:delete-bot"
});

class DesktopStateStore {
  private state: AppState = initialAppState();
  private filePath: string | null = null;
  private writes: Promise<void> = Promise.resolve();
  private syncPromise: Promise<void> | null = null;
  private syncRetryTimer: NodeJS.Timeout | null = null;
  private syncRetryMs = 1_000;
  private revision = 0;
  private dirty = false;
  private generation = 0;

  constructor(
    private readonly accountsDirectory: string,
    private readonly remote: DesktopOAuthController,
    private readonly secureStorage: typeof safeStorage
  ) {}

  async activateAccount(
    accountId: string | null,
    options: { claimGuest?: boolean; legacyFilePath?: string; loadRemote?: boolean } = {}
  ): Promise<AppState> {
    await this.writes;
    if (this.syncRetryTimer) clearTimeout(this.syncRetryTimer);
    this.syncRetryTimer = null;
    const guestState = structuredClone(this.state);
    if (!accountId) {
      // An in-flight upload is generation/file scoped and cannot mutate this
      // signed-out view. Do not make logout wait on a slow or sleeping API.
      this.filePath = null;
      this.state = initialAppState();
      if (options.legacyFilePath) {
        try {
          this.state = normalizeAppState(JSON.parse(
            await readFile(options.legacyFilePath, "utf8")
          ));
        } catch {}
      }
      this.revision = 0;
      this.dirty = false;
      this.generation = 0;
      return structuredClone(this.state);
    }
    if (this.syncPromise) await this.syncPromise;
    const scope = createHash("sha256").update(accountId).digest("hex");
    const nextFilePath = path.join(this.accountsDirectory, `${scope}.bin`);
    const previousPlaintextPath = path.join(this.accountsDirectory, `${scope}.json`);
    let loaded: AppState | null = null;
    let loadedRevision = 0;
    let loadedDirty = false;
    let migratedLegacyFilePath = "";
    try {
      if (!await this.secureStorage.isAsyncEncryptionAvailable()) throw new Error("El almacenamiento seguro del sistema no está disponible.");
      const parsed: unknown = JSON.parse((await this.secureStorage.decryptStringAsync(await readFile(nextFilePath))).result);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && "state" in parsed) {
        const envelope = parsed as Record<string, unknown>;
        loaded = normalizeAppState(envelope.state);
        loadedRevision = typeof envelope.serverRevision === "number"
          && Number.isSafeInteger(envelope.serverRevision)
          && envelope.serverRevision >= 0
          ? envelope.serverRevision
          : 0;
        loadedDirty = envelope.dirty === true;
      } else {
        loaded = normalizeAppState(parsed);
      }
    } catch {}
    if (!loaded) {
      try {
        const parsed: unknown = JSON.parse(await readFile(previousPlaintextPath, "utf8"));
        const envelope = parsed as Record<string, unknown>;
        loaded = normalizeAppState(envelope && "state" in envelope ? envelope.state : parsed);
        loadedRevision = typeof envelope.serverRevision === "number" ? Math.max(0, envelope.serverRevision) : 0;
        loadedDirty = envelope.dirty === true;
        migratedLegacyFilePath = previousPlaintextPath;
      } catch {}
    }
    if (!loaded && options.legacyFilePath) {
      try {
        loaded = normalizeAppState(JSON.parse(await readFile(options.legacyFilePath, "utf8")));
        migratedLegacyFilePath = options.legacyFilePath;
      } catch {}
    }
    if (!loaded && options.claimGuest && hasUserState(guestState)) loaded = guestState;
    this.filePath = nextFilePath;
    this.state = loaded ?? initialAppState();
    this.revision = loadedRevision;
    // A legacy plaintext snapshot has never been acknowledged by the remote
    // account-state service. This must remain dirty even when startup skips
    // the network, otherwise the first remote refresh can replace the local
    // bots instead of uploading them.
    this.dirty = loadedDirty || Boolean(migratedLegacyFilePath);
    this.generation = 0;
    if (options.loadRemote !== false) {
      try {
        const server = await this.remote.loadAccountState();
        const shouldMergeLocal = Boolean(loaded && hasUserState(loaded) && (loadedRevision === 0 || loadedDirty));
        this.state = shouldMergeLocal ? mergeAppStates(server.state, loaded!) : server.state;
        this.revision = server.revision;
        this.dirty = shouldMergeLocal;
        if (shouldMergeLocal) await this.syncRemote();
      } catch (error) {
        this.dirty = this.dirty || (this.revision === 0 && hasUserState(this.state));
        console.error(`[account-state] No fue posible cargar el estado remoto: ${errorMessage(error)}`);
      }
    }
    if (loaded || hasUserState(this.state)) {
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

  async refreshRemote(): Promise<AppState> {
    await this.writes;
    if (!this.filePath) return this.snapshot();
    const filePath = this.filePath;
    if (this.syncPromise) await this.syncPromise;
    if (this.filePath !== filePath) return this.snapshot();
    const server = await this.remote.loadAccountState();
    let result = structuredClone(this.state);
    this.writes = this.writes.then(async () => {
      if (this.filePath !== filePath || server.revision <= this.revision) {
        result = structuredClone(this.state);
        return;
      }
      if (this.dirty) {
        this.state = mergeAppStates(server.state, this.state);
        this.dirty = true;
        this.generation += 1;
      } else {
        this.state = server.state;
        this.dirty = false;
      }
      this.revision = server.revision;
      await this.persist(this.state);
      result = structuredClone(this.state);
    });
    await this.writes;
    if (this.dirty) this.scheduleRemoteSync();
    return result;
  }

  async update(mutator: (current: AppState) => AppState): Promise<AppState> {
    let result = structuredClone(this.state);
    this.writes = this.writes.then(async () => {
      this.state = normalizeAppState(mutator(structuredClone(this.state)));
      this.dirty = true;
      this.generation += 1;
      await this.persist(this.state);
      result = structuredClone(this.state);
    });
    await this.writes;
    this.scheduleRemoteSync();
    return result;
  }

  async reconcileConnections(connections: Awaited<ReturnType<DesktopOAuthController["snapshot"]>>): Promise<AppState> {
    if (!connections.account.connected) return this.snapshot();
    const connected = normalizeConnectorIds(
      connections.connectors.filter((item) => item.connected).map((item) => item.connectorId)
    ).sort();
    const current = await this.snapshot();
    const connectedSet = new Set(connected);
    const next = normalizeAppState({
      ...current,
      selectedConnectorIds: connected,
      bots: current.bots.map((bot) => {
        const connectorIds = bot.connectorIds.filter((item) => connectedSet.has(item));
        return connectorIds.length === bot.connectorIds.length
          ? bot
          : {
            ...bot,
            connectorIds,
            updatedAt: new Date().toISOString(),
            connectorAssignmentRevision: new Date().toISOString()
          };
      })
    });
    return JSON.stringify(current) === JSON.stringify(next) ? current : this.update(() => next);
  }

  async deleteActiveAccount(): Promise<AppState> {
    await this.writes;
    if (this.syncRetryTimer) clearTimeout(this.syncRetryTimer);
    this.syncRetryTimer = null;
    const accountFilePath = this.filePath;
    this.filePath = null;
    this.state = initialAppState();
    this.revision = 0;
    this.dirty = false;
    this.generation = 0;
    if (accountFilePath) await rm(accountFilePath, { force: true });
    return structuredClone(this.state);
  }

  private async persist(snapshot: AppState): Promise<void> {
    if (!this.filePath) return;
    if (!await this.secureStorage.isAsyncEncryptionAvailable()) {
      throw new Error("Desbloquea la sesión del sistema para guardar tus conversaciones.");
    }
    await mkdir(path.dirname(this.filePath), { recursive: true });
    const temporaryPath = `${this.filePath}.${process.pid}.tmp`;
    const cleartext = `${JSON.stringify({
      state: snapshot,
      serverRevision: this.revision,
      dirty: this.dirty
    }, null, 2)}\n`;
    await writeFile(temporaryPath, await this.secureStorage.encryptStringAsync(cleartext), { mode: 0o600 });
    await rename(temporaryPath, this.filePath);
  }

  private async syncRemote(): Promise<void> {
    while (this.filePath && this.dirty) {
      await this.writes;
      const filePath = this.filePath;
      const generation = this.generation;
      const snapshot = structuredClone(this.state);
      const revision = this.revision;
      let saved: Awaited<ReturnType<DesktopOAuthController["saveAccountState"]>>;
      try {
        saved = await this.remote.saveAccountState(snapshot, revision);
      } catch (error) {
        if (!(error instanceof AccountStateConflictError)) throw error;
        this.writes = this.writes.then(async () => {
          if (this.filePath !== filePath) return;
          this.state = mergeAppStates(error.current.state, this.state);
          this.revision = error.current.revision;
          this.dirty = true;
          this.generation += 1;
          await this.persist(this.state);
        });
        await this.writes;
        continue;
      }
      this.writes = this.writes.then(async () => {
        if (this.filePath !== filePath) return;
        this.revision = saved.revision;
        if (this.generation === generation) {
          this.state = saved.state;
          this.dirty = false;
        }
        await this.persist(this.state);
      });
      await this.writes;
    }
  }

  private scheduleRemoteSync(delayMs = 0): void {
    if (!this.filePath || !this.dirty || this.syncPromise || this.syncRetryTimer) return;
    if (delayMs > 0) {
      this.syncRetryTimer = setTimeout(() => {
        this.syncRetryTimer = null;
        this.scheduleRemoteSync();
      }, delayMs);
      this.syncRetryTimer.unref();
      return;
    }
    this.syncPromise = this.syncRemote()
      .then(() => { this.syncRetryMs = 1_000; })
      .catch((error) => {
        console.error(`[account-state] No fue posible sincronizar el cambio: ${errorMessage(error)}`);
        this.syncRetryMs = Math.min(this.syncRetryMs * 2, 30_000);
      })
      .finally(() => {
        this.syncPromise = null;
        if (this.dirty) this.scheduleRemoteSync(this.syncRetryMs);
      });
  }
}

function hasUserState(state: AppState): boolean {
  return state.onboardingCompleted || state.bots.length > 0 || state.selectedConnectorIds.length > 0;
}

function mergeAppStates(server: AppState, local: AppState): AppState {
  const deletedBotIds = [...new Set([...server.deletedBotIds, ...local.deletedBotIds])].slice(-1000);
  const deletedBots = new Set(deletedBotIds);
  const bots = new Map(server.bots.map((bot) => [bot.id, bot]));
  for (const localBot of local.bots) {
    const serverBot = bots.get(localBot.id);
    if (!serverBot) {
      bots.set(localBot.id, localBot);
      continue;
    }
    const messages = new Map(serverBot.messages.map((message) => [message.id, message]));
    for (const message of localBot.messages) messages.set(message.id, message);
    const workflows = new Map(serverBot.workflows.map((workflow) => [workflow.id, workflow]));
    for (const workflow of localBot.workflows) {
      const existing = workflows.get(workflow.id);
      if (!existing || Date.parse(workflow.updatedAt) >= Date.parse(existing.updatedAt)) {
        workflows.set(workflow.id, workflow);
      }
    }
    const profile = newerBotDomain(localBot, serverBot, "profileRevision");
    const connectors = newerBotDomain(localBot, serverBot, "connectorAssignmentRevision");
    const notifications = newerBotDomain(localBot, serverBot, "notificationRevision");
    bots.set(localBot.id, {
      ...profile,
      connectorIds: normalizeConnectorIds(connectors.connectorIds),
      notificationsEnabled: notifications.notificationsEnabled,
      messages: [...messages.values()]
        .sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt))
        .slice(-200),
      workflows: [...workflows.values()].slice(-50),
      updatedAt: laterDate(localBot.updatedAt, serverBot.updatedAt),
      profileRevision: laterDate(localBot.profileRevision, serverBot.profileRevision),
      connectorAssignmentRevision: laterDate(localBot.connectorAssignmentRevision, serverBot.connectorAssignmentRevision),
      notificationRevision: laterDate(localBot.notificationRevision, serverBot.notificationRevision),
      conversationRevision: laterDate(localBot.conversationRevision, serverBot.conversationRevision),
      workflowRevision: laterDate(localBot.workflowRevision, serverBot.workflowRevision)
    });
  }
  const mergedBots = [...bots.values()].filter((bot) => !deletedBots.has(bot.id)).slice(0, 100);
  const activeBotId = local.activeBotId && mergedBots.some((bot) => bot.id === local.activeBotId)
    ? local.activeBotId
    : server.activeBotId;
  const pendingRuns = new Map(server.pendingRuns.map((run) => [run.idempotencyKey, run]));
  for (const run of local.pendingRuns) pendingRuns.set(run.idempotencyKey, run);
  const livePendingRuns = [...pendingRuns.values()].filter((run) => {
    const bot = mergedBots.find((candidate) => candidate.id === run.botId);
    const turnIndex = bot?.messages.findIndex((message) => message.id === run.turnId) ?? -1;
    return Boolean(bot) && turnIndex >= 0
      && !bot!.messages.slice(turnIndex + 1).some((message) => message.role === "assistant");
  });
  return normalizeAppState({
    version: 2,
    onboardingCompleted: server.onboardingCompleted || local.onboardingCompleted,
    // Installation/revocation is owned by the server-side OAuth snapshot.
    // Local set union (or always preferring local) resurrects revoked access.
    selectedConnectorIds: normalizeConnectorIds(server.selectedConnectorIds),
    bots: mergedBots,
    deletedBotIds,
    activeBotId,
    pendingRuns: livePendingRuns
  });
}

function laterDate(left: string, right: string): string {
  return Date.parse(left) >= Date.parse(right) ? left : right;
}

function newerBotDomain(
  left: AppState["bots"][number],
  right: AppState["bots"][number],
  revision: "profileRevision" | "connectorAssignmentRevision" | "notificationRevision"
): AppState["bots"][number] {
  return Date.parse(left[revision]) >= Date.parse(right[revision]) ? left : right;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

app.setName("Agent Genia");
app.setAppUserModelId("com.agentgenia.desktop");

let mainWindow: BrowserWindow | null = null;
let computerWindow: BrowserWindow | null = null;
const issuedComputerViewerUrls = new Set<string>();
let stateStore: DesktopStateStore;
let oauthController: DesktopOAuthController;
let localAgentRuntime: LocalAgentRuntime | null = null;
let teachRecordingsDirectory = "";
let activeTeachRecording: ActiveTeachRecording | null = null;
const smokeTest = process.argv.includes("--smoke-test");
const smokeUserDataPath = smokeTest
  ? mkdtempSync(path.join(tmpdir(), "agentgenia-smoke-"))
  : null;
if (smokeUserDataPath) app.setPath("userData", smokeUserDataPath);
const hasSingleInstanceLock = smokeTest || app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) app.quit();

interface ActiveTeachRecording {
  phase: "recording" | "processing";
  botId: string;
  botName: string;
  entryPoint: TeachEntryPoint;
  startedAt: string;
  recordingId: string;
  accountScope: string;
}

const IDLE_TEACH_STATUS: TeachRecordingStatus = Object.freeze({
  phase: "idle",
  botId: "",
  botName: "",
  entryPoint: "",
  startedAt: ""
});

app.on("second-instance", () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
});

function registerDesktopIpc(): void {
  ipcMain.handle(CHANNELS.bootstrap, () => stateStore.snapshot());
  ipcMain.handle(CHANNELS.refreshAccountState, () => stateStore.refreshRemote());
  ipcMain.handle(CHANNELS.connectionSnapshot, async () => {
    const connections = await oauthController.snapshot();
    if (connections.account.connected) {
      await stateStore.refreshRemote().catch((error) => {
        console.error(`[account-state] No fue posible refrescar el estado remoto: ${errorMessage(error)}`);
      });
    }
    await stateStore.reconcileConnections(connections);
    return connections;
  });
  ipcMain.handle(CHANNELS.signIn, async () => {
    const wasSignedOut = !(await oauthController.accountId());
    const connections = await oauthController.signIn();
    await stateStore.activateAccount(await oauthController.accountId(), { claimGuest: wasSignedOut });
    await stateStore.reconcileConnections(connections);
    return connections;
  });
  ipcMain.handle(CHANNELS.signOut, async () => {
    activeTeachRecording = null;
    issuedComputerViewerUrls.clear();
    computerWindow?.close();
    // Flush or safely retain the account-scoped state while the access token
    // still exists. Clearing OAuth first made dirty state wait on a doomed 401.
    await stateStore.activateAccount(null);
    return oauthController.signOut();
  });
  ipcMain.handle(CHANNELS.deleteAccount, async () => {
    activeTeachRecording = null;
    issuedComputerViewerUrls.clear();
    computerWindow?.close();
    const accountId = await oauthController.accountId();
    const connections = await oauthController.deleteAccount();
    const cleanup = await Promise.allSettled([
      stateStore.deleteActiveAccount(),
      accountId
        ? rm(path.join(teachRecordingsDirectory, accountScope(accountId)), {
          recursive: true,
          force: true
        })
        : Promise.resolve()
    ]);
    for (const result of cleanup) {
      if (result.status === "rejected") {
        console.error(`[account-delete] No fue posible borrar todos los datos locales: ${errorMessage(result.reason)}`);
      }
    }
    return connections;
  });
  ipcMain.handle(CHANNELS.connectConnector, async (_event, connectorId: unknown) => {
    if (typeof connectorId !== "string") throw new Error("Conector inválido.");
    const connections = await oauthController.connect(connectorId);
    await stateStore.reconcileConnections(connections);
    await stateStore.update((state) => ({
      ...state,
      bots: state.bots.map((bot) => bot.id === state.activeBotId
        ? {
          ...bot,
          connectorIds: normalizeConnectorIds([...bot.connectorIds, connectorId]),
          updatedAt: new Date().toISOString(),
          connectorAssignmentRevision: new Date().toISOString()
        }
        : bot)
    }));
    return connections;
  });
  ipcMain.handle(CHANNELS.disconnectConnector, async (_event, connectorId: unknown) => {
    if (typeof connectorId !== "string") throw new Error("Conector inválido.");
    const connections = await oauthController.disconnect(connectorId);
    await stateStore.reconcileConnections(connections);
    return connections;
  });
  ipcMain.handle(CHANNELS.billingSnapshot, () => oauthController.billingStatus());
  ipcMain.handle(CHANNELS.startCheckout, (_event, tier: unknown) => {
    if (tier !== "basic" && tier !== "pro" && tier !== "business") throw new Error("Plan inválido.");
    return oauthController.startCheckout(tier);
  });
  ipcMain.handle(CHANNELS.openBillingPortal, () => oauthController.openBillingPortal());
  ipcMain.handle(CHANNELS.whatsappStatus, () => oauthController.whatsAppStatus());
  ipcMain.handle(CHANNELS.startWhatsappLink, () => oauthController.startWhatsAppLink());
  ipcMain.handle(CHANNELS.unlinkWhatsapp, () => oauthController.unlinkWhatsApp());
  ipcMain.handle(CHANNELS.computerStatus, (_event, botId: unknown) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    return oauthController.computerStatus(botId);
  });
  ipcMain.handle(CHANNELS.ensureComputer, async (_event, botId: unknown, botName: unknown) => {
    if (typeof botId !== "string" || typeof botName !== "string") throw new Error("Bot inválido.");
    const snapshot = await oauthController.ensureComputer(botId, botName);
    if (snapshot.viewer_url) rememberComputerViewerUrl(snapshot.viewer_url);
    return snapshot;
  });
  ipcMain.handle(CHANNELS.handBackComputer, async (_event, botId: unknown) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    const snapshot = await oauthController.handBackComputer(botId);
    issuedComputerViewerUrls.clear();
    computerWindow?.close();
    return snapshot;
  });
  ipcMain.handle(CHANNELS.deleteComputer, async (_event, botId: unknown) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    const result = await oauthController.deleteComputer(botId);
    issuedComputerViewerUrls.clear();
    computerWindow?.close();
    return result;
  });
  ipcMain.handle(CHANNELS.openComputerViewer, (_event, value: unknown) => {
    if (typeof value !== "string") throw new Error("URL de computadora inválida.");
    const url = safeComputerViewerUrl(value);
    if (!issuedComputerViewerUrls.delete(url)) throw new Error("El viewer ya expiró o no fue emitido para esta sesión.");
    openComputerWindow(url);
  });
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
    if (state.bots.length >= 100) throw new Error("Puedes tener como máximo 100 bots. Elimina uno antes de crear otro.");
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
  ipcMain.handle(CHANNELS.warmBotAgent, async (_event, botId: unknown) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    await oauthController.warmAgent(botId);
  });
  ipcMain.handle(CHANNELS.runBotAgent, async (
    event,
    botId: unknown,
    promptValue: unknown,
    initialValue?: unknown,
    actionValue?: unknown
  ) => {
    if (typeof botId !== "string") throw new Error("Bot inválido.");
    const prompt = typeof promptValue === "string" ? promptValue.replace(/\s+/g, " ").trim().slice(0, 20_000) : "";
    const initial = initialValue === true;
    const action = normalizeBotWidgetAction(actionValue);
    if (actionValue !== undefined && !action) throw new Error("La aprobación no es válida.");
    if (!initial && !prompt) throw new Error("Escribe un mensaje.");
    const before = await stateStore.snapshot();
    const bot = before.bots.find((item) => item.id === botId);
    if (!bot) throw new Error("No encontramos ese bot.");
    if (initial && bot.messages.length) return before;
    const connectorIds = normalizeConnectorIds(bot.connectorIds);
    const turnId = initial ? `initial-${botId}` : randomUUID();
    if (!initial) {
      const createdAt = new Date().toISOString();
      await stateStore.update((current) => ({
        ...current,
        bots: current.bots.map((item) => item.id === botId
          ? {
            ...item,
            messages: [...item.messages, {
              id: turnId,
              role: "user" as const,
              text: prompt,
              createdAt
            }].slice(-200),
            updatedAt: createdAt,
            conversationRevision: createdAt
          }
          : item),
        activeBotId: botId,
        pendingRuns: [...current.pendingRuns.filter((run) => run.idempotencyKey !== turnId), {
          turnId,
          idempotencyKey: turnId,
          runId: "",
          botId,
          status: "pending" as const,
          submittedAt: createdAt,
          lastRecoveryAt: ""
        }].slice(-100)
      }));
    }
    try {
      const result = await oauthController.runAgent(
        buildBotPrompt({ ...bot, connectorIds }, prompt, initial),
        connectorIds,
        {
          computer: false,
          botId,
          idempotencyKey: turnId,
          // The first-turn widget is generated by the LLM but needs no tools.
          // Route it through the low-latency non-thinking path as well.
          executionMode: initial ? "chat" : "auto",
          chatPrompt: initial
            ? buildBotPrompt({ ...bot, connectorIds }, prompt, true)
            : buildDirectChatPrompt({ ...bot, connectorIds }, prompt),
          routingContext: buildRoutingContext(bot),
          userMessage: prompt,
          approval: action ? { approval_id: action.approvalId, decision: action.decision } : undefined,
          onDelta: (text) => {
            if (!event.sender.isDestroyed()) event.sender.send(CHANNELS.agentDelta, { botId, text });
          }
        }
      );
      const generated = parseAgentAnswer(result.answer);
      if (!generated.text && !generated.widget) throw new Error("El agente no devolvió una respuesta.");
      const now = new Date().toISOString();
      return stateStore.update((current) => {
        const index = current.bots.findIndex((item) => item.id === botId);
        if (index < 0) throw new Error("El bot se eliminó mientras trabajaba.");
        const replyId = initial ? botId : assistantMessageId(turnId);
        const messages = [
          ...current.bots[index].messages.filter((message) => message.id !== replyId),
          {
            id: replyId,
            role: "assistant" as const,
            text: generated.text,
            ...(generated.widget ? { widget: generated.widget } : {}),
            createdAt: now
          }
        ].slice(-200);
        const bots = [...current.bots];
        bots[index] = { ...bots[index], messages, updatedAt: now, conversationRevision: now };
        return {
          ...current,
          bots,
          activeBotId: botId,
          pendingRuns: initial
            ? current.pendingRuns
            : current.pendingRuns.filter((run) => run.idempotencyKey !== turnId)
        };
      });
    } catch (error) {
      // Keep the user's turn on uncertain transport failures. The durable
      // run can still finish and execute external work; deleting the turn
      // encourages a dangerous duplicate submission and loses context.
      throw error;
    }
  });
  ipcMain.handle(CHANNELS.getTeachRecordingStatus, () => teachRecordingStatus());
  ipcMain.handle(CHANNELS.startTeachRecording, async (
    _event,
    botIdValue: unknown,
    entryPointValue: unknown
  ) => {
    const botId = typeof botIdValue === "string" ? botIdValue : "";
    const entryPoint = normalizeTeachEntryPoint(entryPointValue);
    const accountId = await oauthController.accountId();
    if (!accountId) throw new Error("Inicia sesión antes de enseñar una tarea.");
    const bot = (await stateStore.snapshot()).bots.find((item) => item.id === botId);
    if (!bot) throw new Error("No encontramos ese bot.");
    if (activeTeachRecording) {
      if (activeTeachRecording.botId === botId) return teachRecordingStatus();
      throw new Error(`Ya se está grabando la computadora de ${activeTeachRecording.botName}.`);
    }
    activeTeachRecording = {
      phase: "recording",
      botId,
      botName: bot.name,
      entryPoint,
      startedAt: new Date().toISOString(),
      recordingId: randomUUID(),
      accountScope: accountScope(accountId)
    };
    return teachRecordingStatus();
  });
  ipcMain.handle(CHANNELS.discardTeachRecording, (_event, botIdValue: unknown) => {
    const botId = typeof botIdValue === "string" ? botIdValue : "";
    if (activeTeachRecording && activeTeachRecording.botId !== botId) {
      throw new Error(`Ya se está grabando la computadora de ${activeTeachRecording.botName}.`);
    }
    activeTeachRecording = null;
    return teachRecordingStatus();
  });
  ipcMain.handle(CHANNELS.stopTeachRecording, async (
    _event,
    botIdValue: unknown,
    captureValue: unknown
  ) => {
    const botId = typeof botIdValue === "string" ? botIdValue : "";
    const recording = activeTeachRecording;
    if (!recording || recording.botId !== botId || recording.phase !== "recording") {
      throw new Error("No hay una grabación activa para este bot.");
    }
    const accountId = await oauthController.accountId();
    if (!accountId || accountScope(accountId) !== recording.accountScope) {
      activeTeachRecording = null;
      throw new Error("La cuenta cambió durante la grabación. Vuelve a intentarlo.");
    }
    const capture = normalizeTeachCapture(captureValue);
    recording.phase = "processing";
    let storedRecording = false;
    try {
      const before = await stateStore.snapshot();
      const bot = before.bots.find((item) => item.id === botId);
      if (!bot) throw new Error("No encontramos ese bot.");
      if (bot.workflows.length >= 50) throw new Error("Este bot ya tiene 50 tareas aprendidas. Elimina una antes de grabar otra.");
      const extracted = await oauthController.teachWorkflow(bot.name, capture.frames, capture.durationMs);
      const workflow = createBotWorkflow(
        extracted,
        randomUUID(),
        recording.recordingId,
        capture.videoBytes.byteLength ? capture.mimeType : ""
      );
      if (capture.videoBytes.byteLength && capture.mimeType) {
        await saveTeachRecording(recording, capture);
        storedRecording = true;
      }
      const now = new Date().toISOString();
      return await stateStore.update((current) => {
        const index = current.bots.findIndex((item) => item.id === botId);
        if (index < 0) throw new Error("El bot se eliminó mientras aprendía.");
        const bots = [...current.bots];
        bots[index] = {
          ...bots[index],
          workflows: [...bots[index].workflows, workflow].slice(-50),
          messages: [...bots[index].messages, {
            id: randomUUID(),
            role: "assistant" as const,
            text: `Aprendí “${workflow.title}”. Ya puedo volver a ejecutar sus ${workflow.steps.length} pasos.`,
            createdAt: now
          }].slice(-200),
          updatedAt: now,
          workflowRevision: now,
          conversationRevision: now
        };
        return { ...current, bots, activeBotId: botId };
      });
    } catch (error) {
      if (storedRecording) await deleteTeachRecording(recording.accountScope, recording.recordingId, capture.mimeType);
      throw error;
    } finally {
      activeTeachRecording = null;
    }
  });
  ipcMain.handle(CHANNELS.runBotWorkflow, async (
    _event,
    botIdValue: unknown,
    workflowIdValue: unknown
  ) => {
    const botId = typeof botIdValue === "string" ? botIdValue : "";
    const workflowId = typeof workflowIdValue === "string" ? workflowIdValue : "";
    const before = await stateStore.snapshot();
    const bot = before.bots.find((item) => item.id === botId);
    const workflow = bot?.workflows.find((item) => item.id === workflowId);
    if (!bot || !workflow) throw new Error("No encontramos esa tarea aprendida.");
    const connectorIds = normalizeConnectorIds(bot.connectorIds);
    const result = await oauthController.runAgent(
      buildWorkflowRunPrompt(bot, workflow),
      connectorIds,
      {
        computer: true,
        botId,
        userMessage: `Ejecuta la tarea aprendida: ${workflow.title}`,
        idempotencyKey: randomUUID()
      }
    );
    const generated = parseAgentAnswer(result.answer);
    if (!generated.text) throw new Error("El agente no devolvió un resultado.");
    const now = new Date().toISOString();
    return stateStore.update((current) => {
      const index = current.bots.findIndex((item) => item.id === botId);
      if (index < 0) throw new Error("El bot se eliminó mientras trabajaba.");
      const bots = [...current.bots];
      bots[index] = {
        ...bots[index],
        workflows: bots[index].workflows.map((item) => item.id === workflowId
          ? { ...item, lastRunAt: now, updatedAt: now }
          : item),
        messages: [
          ...bots[index].messages,
          { id: randomUUID(), role: "user" as const, text: `Ejecuta la tarea aprendida: ${workflow.title}`, createdAt: now },
          { id: randomUUID(), role: "assistant" as const, text: generated.text, createdAt: now }
        ].slice(-200),
        updatedAt: now,
        workflowRevision: now,
        conversationRevision: now
      };
      return { ...current, bots, activeBotId: botId };
    });
  });
  ipcMain.handle(CHANNELS.deleteBotWorkflow, async (
    _event,
    botIdValue: unknown,
    workflowIdValue: unknown
  ) => {
    const botId = typeof botIdValue === "string" ? botIdValue : "";
    const workflowId = typeof workflowIdValue === "string" ? workflowIdValue : "";
    const accountId = await oauthController.accountId();
    const before = await stateStore.snapshot();
    const bot = before.bots.find((item) => item.id === botId);
    const workflow = bot?.workflows.find((item) => item.id === workflowId);
    if (!bot || !workflow) throw new Error("No encontramos esa tarea aprendida.");
    const next = await stateStore.update((current) => ({
      ...current,
      bots: current.bots.map((item) => item.id === botId
        ? {
          ...item,
          workflows: item.workflows.filter((candidate) => candidate.id !== workflowId),
          updatedAt: new Date().toISOString(),
          workflowRevision: new Date().toISOString()
        }
        : item),
      activeBotId: botId
    }));
    if (accountId && workflow.recordingMimeType) {
      await deleteTeachRecording(accountScope(accountId), workflow.recordingId, workflow.recordingMimeType).catch((error) => {
        console.error(`[teach-recording] No fue posible borrar la grabación local: ${errorMessage(error)}`);
      });
    }
    return next;
  });
  ipcMain.handle(CHANNELS.setActiveBot, (_event, botId: unknown) => stateStore.update((state) => ({
    ...state,
    activeBotId: typeof botId === "string" && state.bots.some((bot) => bot.id === botId) ? botId : null
  })));
  ipcMain.handle(CHANNELS.deleteBot, async (_event, botId: unknown) => {
    const before = await stateStore.snapshot();
    const removed = typeof botId === "string" ? before.bots.find((bot) => bot.id === botId) : undefined;
    if (!removed) throw new Error("No encontramos ese bot.");
    const accountId = await oauthController.accountId();
    if (activeTeachRecording?.botId === botId) activeTeachRecording = null;
    const next = await stateStore.update((state) => {
      const bots = state.bots.filter((bot) => bot.id !== botId);
      return {
        ...state,
        bots,
        deletedBotIds: [...new Set([...state.deletedBotIds, removed.id])].slice(-1000),
        activeBotId: state.activeBotId === botId ? bots[0]?.id ?? null : state.activeBotId
      };
    });
    issuedComputerViewerUrls.clear();
    computerWindow?.close();
    if (accountId) {
      void Promise.allSettled([
        oauthController.deleteComputer(removed.id),
        ...removed.workflows.map((workflow) => workflow.recordingMimeType
          ? deleteTeachRecording(accountScope(accountId), workflow.recordingId, workflow.recordingMimeType)
          : Promise.resolve())
      ]).then((cleanup) => {
        for (const result of cleanup) {
          if (result.status === "rejected") {
            console.error(`[bot-delete] No fue posible limpiar un recurso asociado: ${errorMessage(result.reason)}`);
          }
        }
      });
    }
    return next;
  });
}

function teachRecordingStatus(): TeachRecordingStatus {
  return activeTeachRecording
    ? {
      phase: activeTeachRecording.phase,
      botId: activeTeachRecording.botId,
      botName: activeTeachRecording.botName,
      entryPoint: activeTeachRecording.entryPoint,
      startedAt: activeTeachRecording.startedAt
    }
    : { ...IDLE_TEACH_STATUS };
}

function normalizeTeachEntryPoint(value: unknown): TeachEntryPoint {
  if (value === "top_bar" || value === "composer_menu" || value === "screen_hover") return value;
  throw new Error("Punto de entrada de grabación inválido.");
}

function accountScope(accountId: string): string {
  return createHash("sha256").update(accountId).digest("hex");
}

function normalizeTeachCapture(value: unknown): TeachCapture {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("La grabación es inválida.");
  const record = value as Record<string, unknown>;
  const durationMs = typeof record.durationMs === "number" && Number.isFinite(record.durationMs)
    ? Math.max(1_000, Math.min(300_000, Math.round(record.durationMs)))
    : 0;
  if (!durationMs) throw new Error("La duración de la grabación es inválida.");
  const frames = Array.isArray(record.frames)
    ? record.frames.slice(0, 12).flatMap((frame): string[] => {
      if (typeof frame !== "string" || frame.length > 1_500_000) return [];
      return /^data:image\/(?:jpeg|png|webp);base64,[a-z0-9+/=]+$/i.test(frame) ? [frame] : [];
    })
    : [];
  if (frames.length < 2) throw new Error("La grabación necesita al menos dos fotogramas legibles.");
  const mimeType = record.mimeType === "video/mp4" ? "video/mp4"
    : record.mimeType === "video/webm" ? "video/webm"
      : "";
  let videoBytes: Uint8Array<ArrayBufferLike> = new Uint8Array();
  if (record.videoBytes instanceof Uint8Array) {
    videoBytes = record.videoBytes;
  } else if (record.videoBytes instanceof ArrayBuffer) {
    videoBytes = new Uint8Array(record.videoBytes);
  } else if (ArrayBuffer.isView(record.videoBytes)) {
    const view = record.videoBytes;
    videoBytes = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
  }
  if (videoBytes.byteLength > 64 * 1024 * 1024) throw new Error("La grabación excede el límite de 64 MB.");
  if (videoBytes.byteLength && !mimeType) throw new Error("El formato de la grabación no es compatible.");
  return { durationMs, frames, mimeType, videoBytes };
}

function teachRecordingExtension(mimeType: TeachCapture["mimeType"]): string {
  return mimeType === "video/mp4" ? ".mp4" : ".webm";
}

async function saveTeachRecording(recording: ActiveTeachRecording, capture: TeachCapture): Promise<void> {
  const directory = path.join(teachRecordingsDirectory, recording.accountScope);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const filePath = path.join(directory, `${recording.recordingId}${teachRecordingExtension(capture.mimeType)}`);
  await writeFile(filePath, capture.videoBytes, { mode: 0o600, flag: "wx" });
}

async function deleteTeachRecording(
  scope: string,
  recordingId: string,
  mimeType: BotWorkflow["recordingMimeType"]
): Promise<void> {
  if (!/^[0-9a-f-]{36}$/i.test(recordingId) || !mimeType) return;
  await rm(path.join(teachRecordingsDirectory, scope, `${recordingId}${teachRecordingExtension(mimeType)}`), { force: true });
}

function configureDisplayMedia(): void {
  session.defaultSession.setDisplayMediaRequestHandler(async (_request, callback) => {
    try {
      const sources = await desktopCapturer.getSources({
        types: ["screen", "window"],
        thumbnailSize: { width: 0, height: 0 },
        fetchWindowIcons: false
      });
      const source = sources.find((item) => item.id.startsWith("screen:")) ?? sources[0];
      callback(source ? { video: source } : {});
    } catch {
      callback({});
    }
  }, { useSystemPicker: true });
}

function buildWorkflowRunPrompt(bot: AppState["bots"][number], workflow: BotWorkflow): string {
  return [
    `Eres ${bot.name}, un agente de Agent Genia.`,
    bot.title ? `Rol: ${bot.title}.` : "",
    bot.description ? `Objetivo: ${bot.description}.` : "",
    bot.connectorIds.length ? `Conectores autorizables: ${bot.connectorIds.join(", ")}.` : "No hay conectores seleccionados.",
    `Ejecuta ahora la tarea aprendida “${workflow.title}”.`,
    workflow.summary ? `Resultado esperado: ${workflow.summary}` : "",
    "Pasos aprendidos, en orden:",
    ...workflow.steps.map((step, index) => `${index + 1}. ${step}`),
    "Si necesitas GUI, shell o archivos, busca primero 'computadora' con connector_search y usa la herramienta computer que active. Usa conectores cuando corresponda.",
    "Si una autorización o dato humano bloquea un paso, detente y explica exactamente qué necesitas.",
    "No inventes que completaste acciones. Devuelve exclusivamente JSON válido: {\"text\":\"resultado visible\",\"widget\":null}."
  ].filter(Boolean).join("\n");
}

function buildBotPrompt(
  bot: AppState["bots"][number],
  userPrompt: string,
  initial: boolean
): string {
  const history = bot.messages.slice(-4).map((message) => (
    `${message.role === "user" ? "Usuario" : bot.name}: ${message.text.slice(0, 2_000)}`
  )).join("\n");
  const profile = [
    `Eres ${bot.name}, un agente de Agent Genia.`,
    bot.title ? `Rol: ${bot.title}.` : "",
    bot.description ? `Objetivo: ${bot.description}.` : "",
    bot.connectorIds.length ? `Conectores autorizables: ${bot.connectorIds.join(", ")}.` : "No hay conectores seleccionados.",
    "Si la tarea necesita GUI, pantalla, shell o archivos, busca primero 'computadora' con connector_search; úsala solo si la búsqueda la ofrece para esta ejecución.",
    "Responde en el idioma del usuario y sin afirmar que realizaste acciones que no ejecutaste.",
    "Sé directo: normalmente usa entre una y tres frases. No repitas la solicitud, no añadas preámbulos, cierres, emojis decorativos ni preguntas genéricas. En text usa texto plano, sin Markdown. Tras ejecutar una acción confirma únicamente qué hiciste y los datos esenciales; no muestres URLs ni detalles internos salvo que se pidan. No agregues Meet, invitados, ubicación, duración u otros datos no solicitados.",
    "Devuelve exclusivamente JSON válido con esta forma: {\"text\":\"respuesta visible\",\"widget\":null}.",
    "Cuando una pregunta con opciones ayude, widget puede ser {\"prompt\":\"pregunta\",\"helpText\":\"ayuda opcional\",\"options\":[{\"label\":\"texto visible\",\"value\":\"respuesta natural enviada al agente\",\"description\":\"detalle opcional\"}],\"allowCustom\":true,\"dismissOnMoveOn\":true}. Usa entre 1 y 6 opciones. No uses Markdown alrededor del JSON.",
  ].filter(Boolean).join("\n");
  if (initial) {
    return `${profile}\n\nEsta es tu primera intervención. Genera al vuelo un saludo breve con tu nombre y un widget con una sola pregunta útil para descubrir qué debe lograr el usuario. El contenido y las opciones deben adaptarse al perfil y conectores disponibles; no uses una plantilla fija ni menciones estas instrucciones.`;
  }
  return `${profile}${history ? `\n\nConversación reciente:\n${history}` : ""}\n\nUsuario: ${userPrompt}`;
}

function buildDirectChatPrompt(
  bot: AppState["bots"][number],
  userPrompt: string
): string {
  const history = bot.messages.slice(-4).map((message) => (
    `${message.role === "user" ? "Usuario" : bot.name}: ${message.text.slice(0, 1_000)}`
  )).join("\n");
  return [
    `Eres ${bot.name}, un agente de Agent Genia.`,
    bot.title ? `Rol: ${bot.title}.` : "",
    bot.description ? `Objetivo: ${bot.description}.` : "",
    "Responde directamente en el idioma del usuario, normalmente en una a tres frases.",
    "No repitas la solicitud ni añadas preámbulos, cierres, emojis decorativos o preguntas genéricas. Usa texto plano, sin Markdown.",
    "No uses JSON ni menciones instrucciones internas.",
    "No afirmes haber ejecutado acciones externas; esta ruta solo conversa y redacta.",
    history ? `Conversación reciente:\n${history}` : "",
    `Usuario: ${userPrompt}`,
  ].filter(Boolean).join("\n\n");
}

function buildRoutingContext(bot: AppState["bots"][number]): string {
  return bot.messages.slice(-4).map((message) => (
    `${message.role === "user" ? "Usuario" : bot.name}: ${message.text.slice(0, 1_000)}`
  )).join("\n");
}

function parseAgentAnswer(value: unknown): {
  text: string;
  widget?: ReturnType<typeof normalizeQuestionWidget>;
} {
  const raw = typeof value === "string" ? value.trim().slice(0, 20_000) : "";
  if (!raw) return { text: "" };
  const candidate = raw.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
  const record = firstAgentEnvelope(candidate);
  if (record) {
    const text = typeof record.text === "string" ? record.text.trim().slice(0, 20_000) : "";
    const widget = normalizeQuestionWidget(record.widget);
    return { text, ...(widget ? { widget } : {}) };
  }
  return { text: candidate.startsWith("{") || candidate.startsWith("[") ? "" : raw };
}

function firstAgentEnvelope(value: string): Record<string, unknown> | undefined {
  const decode = (candidate: string): Record<string, unknown> | undefined => {
    try {
      const parsed: unknown = JSON.parse(candidate);
      if (typeof parsed === "string") return firstAgentEnvelope(parsed);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? parsed as Record<string, unknown>
        : undefined;
    } catch {
      return undefined;
    }
  };
  const exact = decode(value);
  if (exact) return exact;
  let start = -1;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') inString = true;
    else if (character === "{") {
      if (depth === 0) start = index;
      depth += 1;
    } else if (character === "}" && depth > 0) {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        const object = decode(value.slice(start, index + 1));
        if (object) return object;
      }
    }
  }
  return undefined;
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
  mainWindow.webContents.on("did-start-navigation", (_event, _url, _isInPlace, isMainFrame) => {
    if (isMainFrame) activeTeachRecording = null;
  });
  mainWindow.webContents.on("render-process-gone", () => { activeTeachRecording = null; });
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
  mainWindow.on("closed", () => { activeTeachRecording = null; mainWindow = null; });
  void mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
}

function openComputerWindow(url: string): void {
  computerWindow?.destroy();
  const allowedOrigin = new URL(url).origin;
  const viewerSession = session.fromPartition(`agentgenia-computer-${randomUUID()}`);
  viewerSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  viewerSession.webRequest.onBeforeSendHeaders((details, callback) => {
    let requestHeaders = details.requestHeaders;
    try {
      if (new URL(details.url).origin === allowedOrigin) {
        requestHeaders = { ...requestHeaders, "X-Daytona-Skip-Preview-Warning": "true" };
      }
    } catch {}
    callback({ requestHeaders });
  });
  computerWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 800,
    minHeight: 600,
    show: false,
    title: "Agent Genia Computer",
    backgroundColor: "#111111",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      session: viewerSession,
      devTools: !app.isPackaged
    }
  });
  computerWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  computerWindow.webContents.on("will-navigate", (event, target) => {
    try {
      if (new URL(target).origin !== allowedOrigin) event.preventDefault();
    } catch {
      event.preventDefault();
    }
  });
  computerWindow.once("ready-to-show", () => computerWindow?.show());
  computerWindow.on("closed", () => { computerWindow = null; });
  void computerWindow.loadURL(url);
}

function rememberComputerViewerUrl(value: string): void {
  const url = safeComputerViewerUrl(value);
  issuedComputerViewerUrls.add(url);
  while (issuedComputerViewerUrls.size > 8) {
    const oldest = issuedComputerViewerUrls.values().next().value;
    if (typeof oldest !== "string") break;
    issuedComputerViewerUrls.delete(oldest);
  }
}

function normalizeBotWidgetAction(value: unknown): BotWidgetAction | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const candidate = value as Record<string, unknown>;
  if (candidate.type !== "approval") return undefined;
  if (candidate.decision !== "approve" && candidate.decision !== "reject") return undefined;
  if (typeof candidate.approvalId !== "string" || !/^apr_[a-zA-Z0-9_-]{8,120}$/.test(candidate.approvalId)) {
    return undefined;
  }
  return { type: "approval", approvalId: candidate.approvalId, decision: candidate.decision };
}

let pendingDesktopRecovery: Promise<void> | null = null;

function recoverPendingDesktopRuns(): Promise<void> {
  if (pendingDesktopRecovery) return pendingDesktopRecovery;
  pendingDesktopRecovery = performPendingDesktopRecovery().finally(() => {
    pendingDesktopRecovery = null;
  });
  return pendingDesktopRecovery;
}

async function performPendingDesktopRecovery(): Promise<void> {
  const snapshot = await stateStore.snapshot();
  for (const pending of snapshot.pendingRuns) {
    const recoveryAt = new Date().toISOString();
    await stateStore.update((current) => ({
      ...current,
      pendingRuns: current.pendingRuns.map((run) => run.idempotencyKey === pending.idempotencyKey
        ? { ...run, status: "recovering" as const, lastRecoveryAt: recoveryAt }
        : run)
    }));
    try {
      const result = await oauthController.recoverAgent(pending.idempotencyKey);
      if (!result || typeof result.answer !== "string") continue;
      const generated = parseAgentAnswer(result.answer);
      if (!generated.text && !generated.widget) continue;
      const now = new Date().toISOString();
      const replyId = assistantMessageId(pending.idempotencyKey);
      await stateStore.update((current) => ({
        ...current,
        bots: current.bots.map((bot) => bot.id === pending.botId
          ? {
            ...bot,
            messages: [...bot.messages.filter((message) => message.id !== replyId), {
              id: replyId,
              role: "assistant" as const,
              text: generated.text,
              ...(generated.widget ? { widget: generated.widget } : {}),
              createdAt: now
            }].slice(-200),
            updatedAt: now,
            conversationRevision: now
          }
          : bot),
        pendingRuns: current.pendingRuns.filter((run) => run.idempotencyKey !== pending.idempotencyKey)
      }));
    } catch (error) {
      console.error(`[agent-recovery] ${errorMessage(error)}`);
    }
  }
}

function assistantMessageId(idempotencyKey: string): string {
  const bytes = createHash("sha256")
    .update(`agentgenia:assistant:${idempotencyKey}`)
    .digest()
    .subarray(0, 16);
  bytes[6] = (bytes[6]! & 0x0f) | 0x50;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const compact = bytes.toString("hex");
  return `${compact.slice(0, 8)}-${compact.slice(8, 12)}-${compact.slice(12, 16)}-` +
    `${compact.slice(16, 20)}-${compact.slice(20, 32)}`;
}

function configureAutoUpdates(): void {
  if (!app.isPackaged || smokeTest || process.env.AGENTGENIA_DISABLE_AUTO_UPDATE === "1") return;
  if (process.platform === "linux" && !process.env.APPIMAGE) return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowDowngrade = false;
  autoUpdater.allowPrerelease = app.getVersion().includes("-");

  const check = (): void => {
    void autoUpdater.checkForUpdatesAndNotify().catch((error: unknown) => {
      const message = error instanceof Error ? error.message : "unknown update error";
      console.error(`[updates] ${message}`);
    });
  };
  setTimeout(check, 15_000).unref();
  setInterval(check, 4 * 60 * 60 * 1_000).unref();
}

if (hasSingleInstanceLock) app.whenReady().then(async () => {
  const userDataPath = app.getPath("userData");
  const wrapperServiceUrl = process.env.WRAPPER_SERVICE_URL?.trim()
    || "https://agentgenia-api.onrender.com";
  oauthController = new DesktopOAuthController({
    baseUrl: wrapperServiceUrl,
    safeStorage,
    userDataPath,
    shell,
    appVersion: app.getVersion()
  });
  stateStore = new DesktopStateStore(path.join(userDataPath, "accounts"), oauthController, safeStorage);
  teachRecordingsDirectory = path.join(userDataPath, "teach-recordings");
  // Create the first BrowserWindow before touching Keychain-backed session
  // material. macOS may need to display an authorization prompt and Electron
  // 43 can otherwise wait for it behind an app that has no visible window.
  await stateStore.activateAccount(null, {
    legacyFilePath: path.join(userDataPath, "desktop-state.json"),
    loadRemote: false
  });
  registerDesktopIpc();
  configureDisplayMedia();
  createWindow();
  if (!smokeTest) {
    // Pi Chrome and Computer Use pull in platform-native runtimes. Load them
    // only for a real signed-in desktop session so startup/smoke checks do not
    // initialize an unused native bridge (notably on Windows CI).
    console.info("[local-runtime] loading");
    const { LocalAgentRuntime } = await import("./local-agent-runtime");
    localAgentRuntime = new LocalAgentRuntime(
      {
        heartbeat: (capabilities, signal, activeJobId) => oauthController.desktopRuntimeHeartbeat(
          capabilities,
          signal,
          activeJobId
        ),
        claim: (capabilities, signal) => oauthController.desktopRuntimeClaim(capabilities, signal),
        complete: (jobId, result, signal) => oauthController.desktopRuntimeComplete(jobId, result, signal)
      },
      path.join(userDataPath, "local-agent-runtime")
    );
    localAgentRuntime.start();
    console.info("[local-runtime] started");
  }
  configureAutoUpdates();
  const startupWindow = mainWindow;
  if (startupWindow && startupWindow.webContents.isLoadingMainFrame()) {
    await new Promise<void>((resolve) => {
      startupWindow.webContents.once("did-finish-load", () => resolve());
    });
  }
  const startupAccountId = await oauthController.accountId();
  if (startupAccountId) {
    await stateStore.activateAccount(startupAccountId, {
      // Open from the encrypted local cache immediately. The renderer refreshes
      // the account in the background, so a sleeping Render service cannot hold
      // the native window hostage during launch.
      loadRemote: false
    });
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.reload();
    void recoverPendingDesktopRuns();
  }
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    void recoverPendingDesktopRuns();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("will-quit", () => {
  void localAgentRuntime?.stop();
  if (smokeUserDataPath) rmSync(smokeUserDataPath, { recursive: true, force: true });
});
