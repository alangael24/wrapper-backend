import { contextBridge, ipcRenderer } from "electron";
import type { BotDraft, BotSetupAnswer, DesktopApi } from "./contracts";

const api: DesktopApi = Object.freeze({
  bootstrap: () => ipcRenderer.invoke("desktop:bootstrap"),
  saveConnectors: (connectorIds: string[], onboardingCompleted?: boolean) => (
    ipcRenderer.invoke("desktop:save-connectors", connectorIds, onboardingCompleted)
  ),
  createBot: (draft: BotDraft) => ipcRenderer.invoke("desktop:create-bot", draft),
  answerBotSetup: (botId: string, answer: BotSetupAnswer) => ipcRenderer.invoke("desktop:answer-bot-setup", botId, answer),
  setActiveBot: (botId: string | null) => ipcRenderer.invoke("desktop:set-active-bot", botId),
  deleteBot: (botId: string) => ipcRenderer.invoke("desktop:delete-bot", botId)
});

contextBridge.exposeInMainWorld("wrapperDesktop", api);
