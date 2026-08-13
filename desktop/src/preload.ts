import { contextBridge, ipcRenderer } from "electron";
import type { BotDraft, BotPatch, DesktopApi, TeachCapture, TeachEntryPoint } from "./contracts";

const api: DesktopApi = Object.freeze({
  bootstrap: () => ipcRenderer.invoke("desktop:bootstrap"),
  connectionSnapshot: () => ipcRenderer.invoke("desktop:connection-snapshot"),
  signIn: () => ipcRenderer.invoke("desktop:sign-in"),
  signOut: () => ipcRenderer.invoke("desktop:sign-out"),
  deleteAccount: () => ipcRenderer.invoke("desktop:delete-account"),
  connectConnector: (connectorId: string) => ipcRenderer.invoke("desktop:connect-connector", connectorId),
  disconnectConnector: (connectorId: string) => ipcRenderer.invoke("desktop:disconnect-connector", connectorId),
  billingSnapshot: () => ipcRenderer.invoke("desktop:billing-snapshot"),
  startCheckout: (tier: "basic" | "pro") => ipcRenderer.invoke("desktop:start-checkout", tier),
  openBillingPortal: () => ipcRenderer.invoke("desktop:open-billing-portal"),
  computerStatus: (botId: string) => ipcRenderer.invoke("desktop:computer-status", botId),
  ensureComputer: (botId: string, botName: string) => ipcRenderer.invoke("desktop:ensure-computer", botId, botName),
  handBackComputer: (botId: string) => ipcRenderer.invoke("desktop:hand-back-computer", botId),
  deleteComputer: (botId: string) => ipcRenderer.invoke("desktop:delete-computer", botId),
  openComputerViewer: (url: string) => ipcRenderer.invoke("desktop:open-computer-viewer", url),
  saveConnectors: (connectorIds: string[], onboardingCompleted?: boolean) => (
    ipcRenderer.invoke("desktop:save-connectors", connectorIds, onboardingCompleted)
  ),
  createBot: (draft: BotDraft) => ipcRenderer.invoke("desktop:create-bot", draft),
  updateBot: (botId: string, patch: BotPatch) => ipcRenderer.invoke("desktop:update-bot", botId, patch),
  runBotAgent: (botId: string, prompt: string, initial?: boolean) => (
    ipcRenderer.invoke("desktop:run-bot-agent", botId, prompt, initial)
  ),
  getTeachRecordingStatus: () => ipcRenderer.invoke("desktop:get-teach-recording-status"),
  startTeachRecording: (botId: string, entryPoint: TeachEntryPoint) => (
    ipcRenderer.invoke("desktop:start-teach-recording", botId, entryPoint)
  ),
  stopTeachRecording: (botId: string, capture: TeachCapture) => (
    ipcRenderer.invoke("desktop:stop-teach-recording", botId, capture)
  ),
  discardTeachRecording: (botId: string) => ipcRenderer.invoke("desktop:discard-teach-recording", botId),
  runBotWorkflow: (botId: string, workflowId: string) => (
    ipcRenderer.invoke("desktop:run-bot-workflow", botId, workflowId)
  ),
  deleteBotWorkflow: (botId: string, workflowId: string) => (
    ipcRenderer.invoke("desktop:delete-bot-workflow", botId, workflowId)
  ),
  setActiveBot: (botId: string | null) => ipcRenderer.invoke("desktop:set-active-bot", botId),
  deleteBot: (botId: string) => ipcRenderer.invoke("desktop:delete-bot", botId)
});

contextBridge.exposeInMainWorld("wrapperDesktop", api);
