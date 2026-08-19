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

// Claims use a server-side long poll. This small pause is only reached after
// an empty long poll and prevents a reconnect loop from spinning locally.
const POLL_MS = 50;
const RETRY_AFTER_ERROR_MS = 5_000;
const HEARTBEAT_MS = 15_000;
const LOCAL_RUN_TIMEOUT_MS = 31 * 60 * 1_000;
const LOCAL_TOOL_IDLE_TIMEOUT_MS = 3 * 60 * 1_000;
const COMPLETION_TIMEOUT_MS = 15_000;
const LOCAL_RUN_KEY_ENV = "AGENTGENIA_LOCAL_RUN_KEY";

type LocalModel = NonNullable<ReturnType<ModelRuntime["getModel"]>>;

interface CachedModelRuntime {
  runtime: ModelRuntime;
  model: LocalModel;
}

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
  heartbeat(
    capabilities: DesktopRuntimeCapabilities,
    signal?: AbortSignal,
    activeJobId?: string
  ): Promise<void>;
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
  private readonly modelRuntimes = new Map<string, Promise<CachedModelRuntime>>();
  private readonly resourceLoaders = new Map<string, Promise<DefaultResourceLoader>>();

  constructor(
    private readonly transport: DesktopRuntimeTransport,
    private readonly workspaceDirectory: string
  ) {}

  start(): void {
    if (this.loop) return;
    this.abortController = new AbortController();
    console.info("[local-runtime] polling enabled");
    this.loop = this.runLoop(this.abortController.signal).finally(() => {
      console.info("[local-runtime] polling stopped");
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
          await this.executeAndComplete(job, capabilities, signal);
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

  private async executeAndComplete(
    job: DesktopRuntimeJob,
    capabilities: DesktopRuntimeCapabilities,
    parentSignal: AbortSignal
  ): Promise<void> {
    const timeout = AbortSignal.timeout(LOCAL_RUN_TIMEOUT_MS);
    const signal = AbortSignal.any([parentSignal, timeout]);
    const heartbeatController = new AbortController();
    const heartbeatSignal = AbortSignal.any([parentSignal, heartbeatController.signal]);
    const heartbeatLoop = this.keepAliveDuringJob(job.id, capabilities, heartbeatSignal);
    try {
      const result = await this.execute(job, signal);
      await this.transport.complete(
        job.id,
        { status: "succeeded", result },
        AbortSignal.timeout(COMPLETION_TIMEOUT_MS)
      );
    } catch (error) {
      const cancelled = parentSignal.aborted;
      await this.transport.complete(job.id, {
        status: cancelled ? "cancelled" : "failed",
        error_code: cancelled ? "desktop_runtime_stopped" : "desktop_runtime_error",
        error_message: errorMessage(error).slice(0, 2000)
      }, AbortSignal.timeout(COMPLETION_TIMEOUT_MS)).catch((completionError) => {
        console.error(`[local-runtime] No fue posible reportar ${job.id}: ${errorMessage(completionError)}`);
      });
    } finally {
      heartbeatController.abort();
      await heartbeatLoop;
    }
  }

  private async keepAliveDuringJob(
    jobId: string,
    capabilities: DesktopRuntimeCapabilities,
    signal: AbortSignal
  ): Promise<void> {
    while (!signal.aborted) {
      await delay(HEARTBEAT_MS, signal).catch(() => undefined);
      if (signal.aborted) return;
      await this.transport.heartbeat(capabilities, signal, jobId).catch((error) => {
        if (!signal.aborted) console.error(`[local-runtime] heartbeat: ${errorMessage(error)}`);
      });
    }
  }

  private async execute(job: DesktopRuntimeJob, signal: AbortSignal): Promise<Record<string, unknown>> {
    const startedAt = performance.now();
    const timings: Record<string, number> = {};
    const mark = (name: string): void => {
      timings[name] = Math.round((performance.now() - startedAt) * 1000) / 1000;
    };
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
    // The run token is a short-lived capability. Resolve it lazily from the
    // current job environment so the expensive model registry can stay warm
    // without retaining a previous job's credential.
    setEnvironment(LOCAL_RUN_KEY_ENV, payload.run_api_key);

    let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | null = null;
    try {
      const { runtime: modelRuntime, model } = await this.modelRuntime(
        payload.model,
        payload.backend_url,
        signal
      );
      mark("model_runtime_ready_ms");

      const resourceLoader = await this.resourceLoader(
        payload.browser,
        payload.computer,
        (payload.connector_ids?.length ?? 0) > 0,
        signal
      );
      mark("resource_loader_ready_ms");
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
      mark("session_ready_ms");
      await session.bindExtensions({
        mode: "rpc",
        uiContext: electronExtensionUi()
      });
      mark("extensions_ready_ms");
      if (payload.browser) {
        // Native authorization is process-scoped, but every new AgentSession
        // must still run the extension command so its tools bind to the
        // already-authorized bridge. The native layer suppresses repeat OS
        // prompts during the active 30-minute grant.
        await promptWithWatchdog(
          session,
          "/chrome authorize 30m",
          signal,
          LOCAL_RUN_TIMEOUT_MS
        );
        mark("chrome_authorized_ms");
      }
      await promptWithWatchdog(
        session,
        payload.prompt,
        signal,
        LOCAL_TOOL_IDLE_TIMEOUT_MS
      );
      mark("prompt_complete_ms");
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
        timings,
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

  private modelRuntime(
    modelId: string,
    backendUrl: string,
    signal: AbortSignal
  ): Promise<CachedModelRuntime> {
    const cacheKey = `${backendUrl.replace(/\/$/, "")}\0${modelId}`;
    const existing = this.modelRuntimes.get(cacheKey);
    if (existing) return withAbort(existing, signal);

    const pending = this.createModelRuntime(modelId, backendUrl);
    this.modelRuntimes.set(cacheKey, pending);
    pending.catch(() => {
      if (this.modelRuntimes.get(cacheKey) === pending) this.modelRuntimes.delete(cacheKey);
    });
    return withAbort(pending, signal);
  }

  private resourceLoader(
    browser: boolean,
    computer: boolean,
    connectors: boolean,
    signal: AbortSignal
  ): Promise<DefaultResourceLoader> {
    const cacheKey = `${browser ? 1 : 0}${computer ? 1 : 0}${connectors ? 1 : 0}`;
    const existing = this.resourceLoaders.get(cacheKey);
    if (existing) return withAbort(existing, signal);
    const pending = this.createResourceLoader(browser, computer, connectors);
    this.resourceLoaders.set(cacheKey, pending);
    pending.catch(() => {
      if (this.resourceLoaders.get(cacheKey) === pending) this.resourceLoaders.delete(cacheKey);
    });
    return withAbort(pending, signal);
  }

  private async createResourceLoader(
    browser: boolean,
    computer: boolean,
    connectors: boolean
  ): Promise<DefaultResourceLoader> {
    const resourceLoader = new DefaultResourceLoader({
      cwd: this.workspaceDirectory,
      agentDir: this.workspaceDirectory,
      extensionFactories: [
        ...(browser ? [piChromeExtension] : []),
        ...(computer ? [computerUseExtension] : []),
        ...(connectors ? [connectorExtension] : [])
      ]
    });
    await resourceLoader.reload();
    return resourceLoader;
  }

  private async createModelRuntime(
    modelId: string,
    backendUrl: string
  ): Promise<CachedModelRuntime> {
    const modelRuntime = await ModelRuntime.create({
      modelsPath: null,
      refreshOnCreate: false
    });
    modelRuntime.registerProvider("wrapper-backend", {
        name: "Agent Genia",
        baseUrl: `${backendUrl.replace(/\/$/, "")}/v1`,
        api: "openai-completions",
        apiKey: `$${LOCAL_RUN_KEY_ENV}`,
        authHeader: true,
        models: [{
          id: modelId,
          name: `${modelId} (Agent Genia)`,
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
    // registerProvider refreshes its registry asynchronously. Await the local,
    // network-free pass once while creating the cached runtime so the first
    // job cannot race getModel(); later jobs reuse the already-ready registry.
    await modelRuntime.refresh({ allowNetwork: false });
    const model = modelRuntime.getModel("wrapper-backend", modelId);
    if (!model) throw new Error(`El modelo local ${modelId} no está disponible.`);
    return { runtime: modelRuntime, model };
  }
}

function withAbort<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  signal.throwIfAborted();
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason ?? new DOMException("La operación fue cancelada.", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", abort);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener("abort", abort);
        reject(error);
      }
    );
  });
}

async function promptWithWatchdog(
  session: Awaited<ReturnType<typeof createAgentSession>>["session"],
  prompt: string,
  signal: AbortSignal,
  idleTimeoutMs: number
): Promise<void> {
  signal.throwIfAborted();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let rejectIdle: (reason: Error) => void = () => undefined;
  let rejectAbort: (reason: unknown) => void = () => undefined;
  const idle = new Promise<never>((_resolve, reject) => { rejectIdle = reject; });
  const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject; });
  const resetIdle = (): void => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      rejectIdle(new Error(`Pi local no tuvo actividad durante ${Math.round(idleTimeoutMs / 1_000)} segundos.`));
    }, idleTimeoutMs);
  };
  const abort = (): void => {
    rejectAbort(signal.reason ?? new DOMException("La operación fue cancelada.", "AbortError"));
  };
  const progressEvents = new Set([
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
    "agent_settled",
    "auto_retry_start",
    "auto_retry_end"
  ]);
  const unsubscribe = session.subscribe((event) => {
    if (progressEvents.has(event.type)) resetIdle();
    if (event.type === "tool_execution_start") {
      console.info(`[local-runtime] tool start ${event.toolName}`);
    } else if (event.type === "tool_execution_end") {
      console.info(`[local-runtime] tool end ${event.toolName} error=${event.isError ? 1 : 0}`);
    }
  });
  signal.addEventListener("abort", abort, { once: true });
  resetIdle();
  try {
    await Promise.race([
      session.prompt(prompt, { source: "rpc" }),
      idle,
      aborted
    ]);
  } catch (error) {
    await session.abort().catch(() => undefined);
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
    signal.removeEventListener("abort", abort);
    unsubscribe();
  }
}

function electronExtensionUi(): ExtensionUIContext {
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
