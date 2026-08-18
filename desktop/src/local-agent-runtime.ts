import path from "node:path";
import { mkdir } from "node:fs/promises";
import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  type ExtensionUIContext
} from "@earendil-works/pi-coding-agent";
import {
  computerUseExtension,
  connectorExtension,
  piChromeExtension
} from "./pi-extension-loader.mjs";
import { dialog } from "electron";
import { app } from "electron";

const POLL_MS = 750;
const RETRY_AFTER_ERROR_MS = 5_000;
const HEARTBEAT_MS = 15_000;
const LOCAL_RUN_TIMEOUT_MS = 31 * 60 * 1_000;

export interface DesktopRuntimeJob {
  id: string;
  kind: "browser" | "computer";
  expires_at: number;
  payload: {
    run_id: string;
    run_api_key: string;
    prompt: string;
    model: string;
    thinking_level: "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
    backend_url: string;
    browser: boolean;
    computer: boolean;
    connector_run_token?: string | null;
    connector_ids?: string[];
  };
}

export interface DesktopRuntimeTransport {
  heartbeat(capabilities: DesktopRuntimeCapabilities, signal?: AbortSignal): Promise<void>;
  claim(capabilities: DesktopRuntimeCapabilities, signal?: AbortSignal): Promise<DesktopRuntimeJob | null>;
  complete(
    jobId: string,
    result: { status: "succeeded"; result: Record<string, unknown> }
      | { status: "failed" | "cancelled"; error_code: string; error_message: string },
    signal?: AbortSignal
  ): Promise<void>;
}

export interface DesktopRuntimeCapabilities {
  browser: boolean;
  computer: boolean;
}

interface AssistantUsage {
  input_tokens: number;
  output_tokens: number;
  cached_read_tokens: number;
  cached_write_tokens: number;
}

export class LocalAgentRuntime {
  private abortController: AbortController | null = null;
  private loop: Promise<void> | null = null;
  private chromeAuthorizedUntil = 0;

  constructor(
    private readonly transport: DesktopRuntimeTransport,
    private readonly workspaceDirectory: string
  ) {}

  start(): void {
    if (this.loop) return;
    this.abortController = new AbortController();
    this.loop = this.runLoop(this.abortController.signal).finally(() => {
      this.loop = null;
      this.abortController = null;
    });
  }

  async stop(): Promise<void> {
    this.abortController?.abort();
    await this.loop?.catch(() => undefined);
  }

  private async runLoop(signal: AbortSignal): Promise<void> {
    const capabilities: DesktopRuntimeCapabilities = {
      browser: true,
      computer: ["darwin", "win32", "linux"].includes(process.platform)
    };
    let nextHeartbeat = 0;
    while (!signal.aborted) {
      let retryDelay = POLL_MS;
      try {
        if (Date.now() >= nextHeartbeat) {
          await this.transport.heartbeat(capabilities, signal);
          nextHeartbeat = Date.now() + HEARTBEAT_MS;
        }
        const job = await this.transport.claim(capabilities, signal);
        if (job) {
          await this.executeAndComplete(job, signal);
          nextHeartbeat = 0;
          continue;
        }
      } catch (error) {
        if (signal.aborted) return;
        // Being signed out or temporarily offline is normal. The authenticated
        // transport will recover on the next poll after sign-in/network resume.
        console.error(`[local-runtime] ${errorMessage(error)}`);
        nextHeartbeat = 0;
        retryDelay = RETRY_AFTER_ERROR_MS;
      }
      await delay(retryDelay, signal).catch(() => undefined);
    }
  }

  private async executeAndComplete(job: DesktopRuntimeJob, parentSignal: AbortSignal): Promise<void> {
    const timeout = AbortSignal.timeout(LOCAL_RUN_TIMEOUT_MS);
    const signal = AbortSignal.any([parentSignal, timeout]);
    try {
      const result = await this.execute(job, signal);
      await this.transport.complete(job.id, { status: "succeeded", result }, signal);
    } catch (error) {
      const cancelled = parentSignal.aborted;
      await this.transport.complete(job.id, {
        status: cancelled ? "cancelled" : "failed",
        error_code: cancelled ? "desktop_runtime_stopped" : "desktop_runtime_error",
        error_message: errorMessage(error).slice(0, 2000)
      }).catch((completionError) => {
        console.error(`[local-runtime] No fue posible reportar ${job.id}: ${errorMessage(completionError)}`);
      });
    }
  }

  private async execute(job: DesktopRuntimeJob, signal: AbortSignal): Promise<Record<string, unknown>> {
    const startedAt = performance.now();
    const payload = job.payload;
    if (!payload.run_api_key.startsWith("agrn_") || !payload.prompt.trim()) {
      throw new Error("El backend entregó una capacidad local inválida.");
    }
    const backendUrl = new URL(payload.backend_url);
    if (backendUrl.protocol !== "https:" && !isLoopback(backendUrl)) {
      throw new Error("El proxy del modelo local debe usar HTTPS.");
    }
    await mkdir(this.workspaceDirectory, { recursive: true, mode: 0o700 });

    const previousEnvironment = new Map<string, string | undefined>();
    const setEnvironment = (name: string, value: string | undefined): void => {
      previousEnvironment.set(name, process.env[name]);
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    };
    setEnvironment("PI_CHROME_BRIDGE_HOST", "127.0.0.1");
    setEnvironment("PI_CHROME_BRIDGE_PORT", "17318");
    setEnvironment(
      "PI_CHROME_EXTENSION_ROOT",
      app.isPackaged
        ? path.join(process.resourcesPath, "pi-runtime", "pi-chrome")
        : path.join(app.getAppPath(), "node_modules", "pi-chrome", "extensions", "chrome-profile-bridge")
    );
    setEnvironment("PI_CONNECTOR_BROKER_URL", payload.backend_url);
    setEnvironment("PI_CONNECTOR_RUN_TOKEN", payload.connector_run_token ?? undefined);
    setEnvironment("PI_CONNECTOR_IDS", JSON.stringify(payload.connector_ids ?? []));
    setEnvironment("PI_COMPUTER_ENABLED", payload.computer ? "1" : "0");

    let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | null = null;
    try {
      const modelRuntime = await ModelRuntime.create({
        modelsPath: null,
        refreshOnCreate: false,
        signal
      });
      modelRuntime.registerProvider("wrapper-backend", {
        name: "Agent Genia",
        baseUrl: `${payload.backend_url.replace(/\/$/, "")}/v1`,
        api: "openai-completions",
        authHeader: true,
        models: [{
          id: payload.model,
          name: `${payload.model} (Agent Genia)`,
          reasoning: true,
          // The bundled DeepSeek provider is text-only. Pi Chrome and the
          // semantic Computer Use path return structured text/tool results.
          input: ["text"],
          cost: { input: 0.14, output: 0.28, cacheRead: 0.0028, cacheWrite: 0 },
          contextWindow: 1_000_000,
          maxTokens: 384_000,
          thinkingLevelMap: {
            off: "off",
            minimal: null,
            low: null,
            medium: null,
            high: "high",
            xhigh: null,
            max: "max"
          },
          compat: {
            supportsStore: false,
            supportsDeveloperRole: false,
            supportsReasoningEffort: true,
            maxTokensField: "max_tokens",
            supportsLongCacheRetention: false,
            requiresReasoningContentOnAssistantMessages: true,
            thinkingFormat: "deepseek"
          }
        }]
      });
      await modelRuntime.setRuntimeApiKey("wrapper-backend", payload.run_api_key, { signal });
      const model = modelRuntime.getModel("wrapper-backend", payload.model);
      if (!model) throw new Error(`El modelo local ${payload.model} no está disponible.`);

      const resourceLoader = new DefaultResourceLoader({
        cwd: this.workspaceDirectory,
        agentDir: this.workspaceDirectory,
        extensionFactories: [
          ...(payload.browser ? [piChromeExtension] : []),
          ...(payload.computer ? [computerUseExtension] : []),
          ...((payload.connector_ids?.length ?? 0) > 0 ? [connectorExtension] : [])
        ]
      });
      await resourceLoader.reload();
      ({ session } = await createAgentSession({
        cwd: this.workspaceDirectory,
        agentDir: this.workspaceDirectory,
        modelRuntime,
        model,
        thinkingLevel: payload.thinking_level,
        noTools: "builtin",
        resourceLoader,
        sessionManager: SessionManager.inMemory(this.workspaceDirectory)
      }));
      let chromeAuthorizationGranted = false;
      await session.bindExtensions({
        mode: "rpc",
        uiContext: electronExtensionUi((granted) => {
          chromeAuthorizationGranted = granted;
        })
      });
      if (payload.browser && this.chromeAuthorizedUntil <= Date.now()) {
        // Native pi-chrome keeps authorization scoped to this Pi process and
        // asks the user before touching their authenticated Chrome profile.
        await session.prompt("/chrome authorize 30m", { source: "rpc" });
        if (chromeAuthorizationGranted) {
          this.chromeAuthorizedUntil = Date.now() + 29 * 60 * 1_000;
        }
      }
      await session.prompt(payload.prompt, { source: "rpc" });
      const assistant = [...session.state.messages].reverse().find((message) => message.role === "assistant");
      if (!assistant) throw new Error("Pi terminó sin una respuesta final.");
      const assistantRecord = assistant as unknown as Record<string, unknown>;
      const stopReason = stringValue(assistantRecord.stopReason);
      if (["error", "aborted"].includes(stopReason)) {
        throw new Error(stringValue(assistantRecord.errorMessage) || `Pi terminó con estado ${stopReason}.`);
      }
      const answer = assistantText(assistantRecord).trim();
      if (!answer) throw new Error("Pi terminó sin texto final.");
      return {
        answer,
        model: payload.model,
        duration_seconds: Math.max(0, (performance.now() - startedAt) / 1000),
        usage: assistantUsage(assistantRecord),
        browser: payload.browser
      };
    } finally {
      session?.dispose();
      for (const [name, value] of previousEnvironment) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  }
}

function electronExtensionUi(
  onChromeAuthorization: (granted: boolean) => void
): ExtensionUIContext {
  const ui = {
    async select(title: string, options: string[]): Promise<string | undefined> {
      const response = await dialog.showMessageBox({
        type: "question",
        title: "Agent Genia",
        message: title,
        buttons: [...options, "Cancelar"],
        cancelId: options.length,
        defaultId: 0,
        noLink: true
      });
      return response.response < options.length ? options[response.response] : undefined;
    },
    async confirm(title: string, message: string): Promise<boolean> {
      const response = await dialog.showMessageBox({
        type: "warning",
        title: "Agent Genia",
        message: title,
        detail: message,
        buttons: ["Autorizar", "Cancelar"],
        defaultId: 1,
        cancelId: 1,
        noLink: true
      });
      const granted = response.response === 0;
      if (title.toLowerCase().includes("pi-chrome")) onChromeAuthorization(granted);
      return granted;
    },
    async input(): Promise<string | undefined> { return undefined; },
    notify(message: string, type: "info" | "warning" | "error" = "info"): void {
      if (type === "error") console.error(`[pi] ${message}`);
      else if (type === "warning") console.warn(`[pi] ${message}`);
      else console.info(`[pi] ${message}`);
    },
    onTerminalInput(): () => void { return () => undefined; },
    setStatus(): void {},
    setWorkingMessage(): void {},
    setWorkingVisible(): void {},
    setWorkingIndicator(): void {},
    setHiddenThinkingLabel(): void {},
    setWidget(): void {},
    setFooter(): void {},
    setHeader(): void {},
    setTitle(): void {},
    async custom(): Promise<never> { throw new Error("Esta interacción no está disponible en Electron."); },
    pasteToEditor(): void {},
    setEditorText(): void {},
    getEditorText(): string { return ""; },
    async editor(): Promise<string | undefined> { return undefined; },
    addAutocompleteProvider(): void {},
    setEditorComponent(): void {}
  };
  return ui as unknown as ExtensionUIContext;
}

function assistantText(message: Record<string, unknown>): string {
  const content = message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.flatMap((item) => {
    if (typeof item === "string") return [item];
    if (item && typeof item === "object" && (item as Record<string, unknown>).type === "text") {
      return [stringValue((item as Record<string, unknown>).text)];
    }
    return [];
  }).join("");
}

function assistantUsage(message: Record<string, unknown>): AssistantUsage {
  const usage = message.usage && typeof message.usage === "object"
    ? message.usage as Record<string, unknown>
    : {};
  return {
    input_tokens: numberValue(usage.input ?? usage.input_tokens),
    output_tokens: numberValue(usage.output ?? usage.output_tokens),
    cached_read_tokens: numberValue(usage.cacheRead ?? usage.cached_read_tokens),
    cached_write_tokens: numberValue(usage.cacheWrite ?? usage.cached_write_tokens)
  };
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
}

function isLoopback(url: URL): boolean {
  return url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(done, milliseconds);
    function done(): void {
      signal.removeEventListener("abort", aborted);
      resolve();
    }
    function aborted(): void {
      clearTimeout(timer);
      signal.removeEventListener("abort", aborted);
      reject(signal.reason ?? new DOMException("Abortado", "AbortError"));
    }
    signal.addEventListener("abort", aborted, { once: true });
  });
}
