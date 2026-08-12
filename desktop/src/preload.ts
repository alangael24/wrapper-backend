import { contextBridge, ipcRenderer } from "electron";
import type { BotDraft, BotPatch, DesktopApi } from "./contracts";

const api: DesktopApi = Object.freeze({
  bootstrap: () => ipcRenderer.invoke("desktop:bootstrap"),
  connectionSnapshot: () => ipcRenderer.invoke("desktop:connection-snapshot"),
  signIn: () => ipcRenderer.invoke("desktop:sign-in"),
  signOut: () => ipcRenderer.invoke("desktop:sign-out"),
  connectConnector: (connectorId: string) => ipcRenderer.invoke("desktop:connect-connector", connectorId),
  disconnectConnector: (connectorId: string) => ipcRenderer.invoke("desktop:disconnect-connector", connectorId),
  billingSnapshot: () => ipcRenderer.invoke("desktop:billing-snapshot"),
  startCheckout: (tier: "basic" | "pro") => ipcRenderer.invoke("desktop:start-checkout", tier),
  openBillingPortal: () => ipcRenderer.invoke("desktop:open-billing-portal"),
  saveConnectors: (connectorIds: string[], onboardingCompleted?: boolean) => (
    ipcRenderer.invoke("desktop:save-connectors", connectorIds, onboardingCompleted)
  ),
  createBot: (draft: BotDraft) => ipcRenderer.invoke("desktop:create-bot", draft),
  updateBot: (botId: string, patch: BotPatch) => ipcRenderer.invoke("desktop:update-bot", botId, patch),
  runBotAgent: (botId: string, prompt: string, initial?: boolean) => (
    ipcRenderer.invoke("desktop:run-bot-agent", botId, prompt, initial)
  ),
  setActiveBot: (botId: string | null) => ipcRenderer.invoke("desktop:set-active-bot", botId),
  deleteBot: (botId: string) => ipcRenderer.invoke("desktop:delete-bot", botId)
});

contextBridge.exposeInMainWorld("wrapperDesktop", api);
