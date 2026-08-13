import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { app, BrowserWindow, desktopCapturer, ipcMain, safeStorage, session, shell } from "electron";
import { autoUpdater } from "electron-updater";
import {
  type AppState,
  type BotDraft,
  type BotPatch,
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
import { DesktopOAuthController, safeComputerViewerUrl } from "./oauth";

const CHANNELS = Object.freeze({
  bootstrap: "desktop:bootstrap",
  connectionSnapshot: "desktop:connection-snapshot",
  signIn: "desktop:sign-in",
  signOut: "desktop:sign-out",
  deleteAccount: "desktop:delete-account",
  connectConnector: "desktop:connect-connector",
  disconnectConnector: "desktop:disconnect-connector",
  billingSnapshot: "desktop:billing-snapshot",
  startCheckout: "desktop:start-checkout",
  openBillingPortal: "desktop:open-billing-portal",
  computerStatus: "desktop:computer-status",
  ensureComputer: "desktop:ensure-computer",
  handBackComputer: "desktop:hand-back-computer",
  deleteComputer: "desktop:delete-computer",
  openComputerViewer: "desktop:open-computer-viewer",
  saveConnectors: "desktop:save-connectors",
  createBot: "desktop:create-bot",
  updateBot: "desktop:update-bot",
  runBotAgent: "desktop:run-bot-agent",
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

  async deleteActiveAccount(): Promise<AppState> {
    await this.writes;
    const accountFilePath = this.filePath;
    this.filePath = null;
    this.state = initialAppState();
    if (accountFilePath) await rm(accountFilePath, { force: true });
    return structuredClone(this.state);
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
app.setAppUserModelId("com.agentgenia.desktop");

let mainWindow: BrowserWindow | null = null;
let computerWindow: BrowserWindow | null = null;
const issuedComputerViewerUrls = new Set<string>();
let stateStore: DesktopStateStore;
let oauthController: DesktopOAuthController;
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
  ipcMain.handle(CHANNELS.connectionSnapshot, () => oauthController.snapshot());
  ipcMain.handle(CHANNELS.signIn, async () => {
    const wasSignedOut = !(await oauthController.accountId());
    const connections = await oauthController.signIn();
    await stateStore.activateAccount(await oauthController.accountId(), { claimGuest: wasSignedOut });
    return connections;
  });
  ipcMain.handle(CHANNELS.signOut, async () => {
    activeTeachRecording = null;
    issuedComputerViewerUrls.clear();
    computerWindow?.close();
    const connections = await oauthController.signOut();
    await stateStore.activateAccount(null);
    return connections;
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
    if (cleanup.some((result) => result.status === "rejected")) {
      throw new Error("La cuenta se eliminó, pero no fue posible borrar todos los datos locales.");
    }
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
      connectorIds,
      { computer: true, botId }
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
      throw new Error(`Recording another agent's computer: ${activeTeachRecording.botName}.`);
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
      throw new Error(`Recording another agent's computer: ${activeTeachRecording.botName}.`);
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
          }].slice(-200)
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
    const connectorIds = normalizeConnectorIds([...before.selectedConnectorIds, ...bot.connectorIds]);
    const result = await oauthController.runAgent(
      buildWorkflowRunPrompt(bot, workflow),
      connectorIds,
      { computer: true, botId }
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
        ].slice(-200)
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
        ? { ...item, workflows: item.workflows.filter((candidate) => candidate.id !== workflowId) }
        : item),
      activeBotId: botId
    }));
    if (accountId && workflow.recordingMimeType) {
      await deleteTeachRecording(accountScope(accountId), workflow.recordingId, workflow.recordingMimeType);
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
    const accountId = await oauthController.accountId();
    if (removed && accountId) {
      await oauthController.deleteComputer(removed.id);
      issuedComputerViewerUrls.clear();
      computerWindow?.close();
    }
    if (activeTeachRecording?.botId === botId) activeTeachRecording = null;
    const next = await stateStore.update((state) => {
      const bots = typeof botId === "string" ? state.bots.filter((bot) => bot.id !== botId) : state.bots;
      return {
        ...state,
        bots,
        activeBotId: state.activeBotId === botId ? bots[0]?.id ?? null : state.activeBotId
      };
    });
    if (accountId && removed) {
      await Promise.all(removed.workflows.map((workflow) => workflow.recordingMimeType
        ? deleteTeachRecording(accountScope(accountId), workflow.recordingId, workflow.recordingMimeType)
        : Promise.resolve()));
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
  const history = bot.messages.slice(-20).map((message) => (
    `${message.role === "user" ? "Usuario" : bot.name}: ${message.text}`
  )).join("\n");
  const profile = [
    `Eres ${bot.name}, un agente de Agent Genia.`,
    bot.title ? `Rol: ${bot.title}.` : "",
    bot.description ? `Objetivo: ${bot.description}.` : "",
    bot.connectorIds.length ? `Conectores autorizables: ${bot.connectorIds.join(", ")}.` : "No hay conectores seleccionados.",
    "Si la tarea necesita GUI, pantalla, shell o archivos, busca primero 'computadora' con connector_search; úsala solo si la búsqueda la ofrece para esta ejecución.",
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
  stateStore = new DesktopStateStore(path.join(userDataPath, "accounts"));
  teachRecordingsDirectory = path.join(userDataPath, "teach-recordings");
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
  configureDisplayMedia();
  createWindow();
  configureAutoUpdates();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("will-quit", () => {
  if (smokeUserDataPath) rmSync(smokeUserDataPath, { recursive: true, force: true });
});
