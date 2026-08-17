import { randomBytes, randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import type { SafeStorage, Shell } from "electron";
import type {
  AccountStateSnapshot,
  AppState,
  AccountConnectionStatus,
  BillingSnapshot,
  BotComputerSnapshot,
  BotWorkflowDraft,
  ConnectorConnectionSnapshot,
  ConnectorConnectionStatus,
  WhatsAppLinkStart,
  WhatsAppStatus
} from "./contracts";
import { CONNECTOR_CATALOG, normalizeAppState } from "./contracts";

const SESSION_REFRESH_SKEW_MS = 60_000;
const ACCOUNT_AUTH_ATTEMPTS = 120;
const CONNECTOR_AUTH_ATTEMPTS = 300;
const OAUTH_POLL_MS = 2_000;
const JSON_REQUEST_TIMEOUT_MS = 30_000;
const AGENT_REQUEST_TIMEOUT_MS = 31 * 60 * 1_000;

interface AccountIdentity {
  id: string;
  email: string;
  name?: string;
  picture?: string;
}

interface AccountSession {
  token: string;
  refreshToken: string;
  expiresAt: number;
  account: AccountIdentity;
}

interface ManagedConnectorPayload {
  managed_connection_id: string;
  connector_id: string;
  account_label?: string;
}

interface JsonRequestOptions {
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: Record<string, unknown>;
  signal?: AbortSignal;
}

class WrapperHttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly payload: Record<string, unknown>
  ) {
    super(message);
    this.name = "WrapperHttpError";
  }
}

export class AccountStateConflictError extends Error {
  constructor(readonly current: AccountStateSnapshot) {
    super("La cuenta cambió en otro dispositivo.");
    this.name = "AccountStateConflictError";
  }
}

export const COMPOSIO_CONNECTOR_IDS: ReadonlySet<string> = new Set(
  CONNECTOR_CATALOG.map((connector) => connector.id)
);
export const REMOTE_CONNECTOR_IDS: ReadonlySet<string> = COMPOSIO_CONNECTOR_IDS;

export class DesktopOAuthController {
  private readonly accountStore: EncryptedJsonStore<AccountSession>;
  private readonly deviceStore: DeviceIdentityStore;
  private readonly client: WrapperServiceClient;
  private readonly managedConnectorSessions: Map<string, ManagedConnectorAccount>;
  private lastConnectorResponse: Record<string, unknown> | null = null;
  private sessionInvalidated = false;

  constructor({
    baseUrl,
    safeStorage,
    userDataPath,
    shell,
    appVersion
  }: {
    baseUrl: string;
    safeStorage: SafeStorage;
    userDataPath: string;
    shell: Shell;
    appVersion: string;
  }) {
    this.accountStore = new EncryptedJsonStore({
      filePath: path.join(userDataPath, "secrets", "agent-genia-account.bin"),
      safeStorage,
      validate: isAccountSession
    });
    this.deviceStore = new DeviceIdentityStore({ safeStorage, userDataPath });
    this.client = new WrapperServiceClient({
      baseUrl,
      accountStore: this.accountStore,
      deviceStore: this.deviceStore,
      appVersion,
      openExternal: (url) => shell.openExternal(url)
    });
    this.managedConnectorSessions = new Map(
      [...REMOTE_CONNECTOR_IDS].map((connectorId) => {
        const definition = CONNECTOR_CATALOG.find((item) => item.id === connectorId)!;
        return [
          connectorId,
          new ManagedConnectorAccount({
            connectorId,
            displayName: definition.name,
            client: this.client,
            shell
          })
        ];
      })
    );
  }

  async snapshot(): Promise<ConnectorConnectionSnapshot> {
    const account = await this.accountStatus();
    let response: Record<string, unknown> | null = null;
    let loadError = "";
    if (account.connected) {
      try {
        response = await this.client.connectors();
        this.lastConnectorResponse = response;
      } catch (error) {
        response = this.lastConnectorResponse;
        loadError = oauthErrorMessage(error);
      }
    } else {
      this.lastConnectorResponse = null;
    }
    const remoteConnectors = Array.isArray(response?.connectors) ? response.connectors : [];
    const remoteById = new Map<string, Record<string, unknown>>(
      remoteConnectors.filter(isRecord).map((item) => [stringValue(item.connector_id), item])
    );
    const connectors = CONNECTOR_CATALOG.map((connector): ConnectorConnectionStatus => {
      const remote = remoteById.get(connector.id);
      const available = remote?.available === true;
      const connected = remote?.connected === true;
      return {
        connectorId: connector.id,
        provider: available ? "composio" : null,
        available,
        connected,
        account: connected ? stringValue(remote?.account) : "",
        reason: connected
          ? loadError ? "Mostrando el último estado conocido; no se pudo actualizar." : ""
          : !account.connected
            ? "Inicia sesión para conectar tu cuenta."
            : loadError && !response
              ? `No se pudo actualizar los conectores: ${loadError}`
            : typeof remote?.reason === "string" && remote.reason
              ? remote.reason
              : available
                ? loadError ? "Mostrando el último estado conocido; vuelve a intentarlo en unos segundos." : "Listo para conectar tu cuenta real."
                : loadError ? `No se pudo actualizar los conectores: ${loadError}` : "Este conector todavía no está configurado."
      };
    });
    return { account, connectors };
  }

  async signIn(signal?: AbortSignal): Promise<ConnectorConnectionSnapshot> {
    await this.client.signIn(shellSafeSignal(signal));
    this.sessionInvalidated = false;
    return this.snapshot();
  }

  async signOut(): Promise<ConnectorConnectionSnapshot> {
    await this.client.signOut();
    this.sessionInvalidated = true;
    this.lastConnectorResponse = null;
    return this.snapshot();
  }

  async deleteAccount(): Promise<ConnectorConnectionSnapshot> {
    await this.client.deleteAccount();
    this.sessionInvalidated = true;
    this.lastConnectorResponse = null;
    return this.snapshot();
  }

  async connect(connectorId: string, signal?: AbortSignal): Promise<ConnectorConnectionSnapshot> {
    if (!await this.accountId()) {
      await this.client.signIn(shellSafeSignal(signal));
      this.sessionInvalidated = false;
    }
    const managed = this.managedConnectorSessions.get(connectorId);
    if (managed) {
      await managed.connect(shellSafeSignal(signal));
      return this.snapshot();
    }
    throw new Error("Conector desconocido.");
  }

  async disconnect(connectorId: string): Promise<ConnectorConnectionSnapshot> {
    const managed = this.managedConnectorSessions.get(connectorId);
    if (managed) {
      await managed.disconnect();
      return this.snapshot();
    }
    throw new Error("Conector desconocido.");
  }

  async billingStatus(signal?: AbortSignal): Promise<BillingSnapshot> {
    return parseBillingSnapshot(await this.client.billing(signal));
  }

  startCheckout(tier: "basic" | "pro" | "business", signal?: AbortSignal): Promise<void> {
    return this.client.startCheckout(tier, signal);
  }

  openBillingPortal(signal?: AbortSignal): Promise<void> {
    return this.client.openBillingPortal(signal);
  }

  whatsAppStatus(signal?: AbortSignal): Promise<WhatsAppStatus> {
    return this.client.whatsAppStatus(signal);
  }

  startWhatsAppLink(signal?: AbortSignal): Promise<WhatsAppLinkStart> {
    return this.client.startWhatsAppLink(signal);
  }

  unlinkWhatsApp(signal?: AbortSignal): Promise<WhatsAppStatus> {
    return this.client.unlinkWhatsApp(signal);
  }

  computerStatus(botId: string, signal?: AbortSignal): Promise<BotComputerSnapshot> {
    return this.client.computerStatus(botId, signal);
  }

  ensureComputer(botId: string, botName: string, signal?: AbortSignal): Promise<BotComputerSnapshot> {
    return this.client.ensureComputer(botId, botName, signal);
  }

  handBackComputer(botId: string, signal?: AbortSignal): Promise<BotComputerSnapshot> {
    return this.client.handBackComputer(botId, signal);
  }

  deleteComputer(botId: string, signal?: AbortSignal): Promise<{ deleted: boolean }> {
    return this.client.deleteComputer(botId, signal);
  }

  runAgent(
    prompt: string,
    connectorIds: string[],
    options: {
      browser?: boolean;
      computer?: boolean;
      botId?: string;
      idempotencyKey?: string;
      executionMode?: "auto" | "agent" | "chat";
      chatPrompt?: string;
      userMessage?: string;
      signal?: AbortSignal;
      onDelta?: (text: string) => void;
    } = {}
  ): Promise<Record<string, unknown>> {
    return this.client.runAgent(prompt, connectorIds, options);
  }

  warmAgent(botId: string, signal?: AbortSignal): Promise<void> {
    return this.client.warmAgent(botId, signal);
  }

  teachWorkflow(botName: string, frames: string[], durationMs: number, signal?: AbortSignal): Promise<BotWorkflowDraft> {
    return this.client.teachWorkflow(botName, frames, durationMs, signal);
  }

  async accountId(): Promise<string | null> {
    if (this.sessionInvalidated) return null;
    return (await this.accountStore.get())?.account.id ?? null;
  }

  loadAccountState(signal?: AbortSignal): Promise<AccountStateSnapshot> {
    return this.client.loadAccountState(signal);
  }

  saveAccountState(
    state: AppState,
    baseRevision: number,
    signal?: AbortSignal
  ): Promise<AccountStateSnapshot> {
    return this.client.saveAccountState(state, baseRevision, signal);
  }

  private async accountStatus(): Promise<AccountConnectionStatus> {
    if (this.sessionInvalidated) return { connected: false, required: true, email: "", name: "" };
    const stored = await this.accountStore.get();
    return stored
      ? { connected: true, required: true, email: stored.account.email, name: stored.account.name ?? "" }
      : { connected: false, required: true, email: "", name: "" };
  }
}

class ManagedConnectorAccount {
  private connectPromise: Promise<void> | null = null;

  constructor(private readonly options: {
    connectorId: string;
    displayName: string;
    client: WrapperServiceClient;
    shell: Shell;
  }) {}

  async connect(signal?: AbortSignal): Promise<void> {
    if (!this.connectPromise) this.connectPromise = this.connectOnce(signal).finally(() => { this.connectPromise = null; });
    await withSignal(this.connectPromise, signal);
  }

  async disconnect(): Promise<void> {
    await this.options.client.disconnectConnector(this.options.connectorId);
  }

  private async connectOnce(signal?: AbortSignal): Promise<void> {
    const provider = await this.options.client.connector(this.options.connectorId, signal);
    if (provider.available !== true) throw new Error(stringValue(provider.reason) || `${this.options.displayName} todavía no está configurado.`);
    const started = await this.options.client.startConnector(this.options.connectorId, signal);
    await this.options.shell.openExternal(safeAuthorizationUrl(stringValue(started.authorize_url)));
    for (let attempt = 0; attempt < CONNECTOR_AUTH_ATTEMPTS; attempt += 1) {
      const status = await this.options.client.connectorStatus(stringValue(started.attempt_id), signal);
      if (status.status === "complete" && isManagedConnectorPayload(status.session)) {
        return;
      }
      if (status.status === "error") throw new Error(stringValue(status.message) || `${this.options.displayName} rechazó la conexión.`);
      await delay(OAUTH_POLL_MS, signal);
    }
    throw new Error(`La conexión con ${this.options.displayName} expiró.`);
  }
}

class WrapperServiceClient {
  private session: { token: string; expiresAt: number } | null = null;
  private refreshPromise: Promise<{ token: string; expiresAt: number }> | null = null;

  constructor(private readonly options: {
    baseUrl: string;
    accountStore: EncryptedJsonStore<AccountSession>;
    deviceStore: DeviceIdentityStore;
    appVersion: string;
    openExternal: (url: string) => Promise<void>;
  }) {
    const url = new URL(options.baseUrl);
    const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
    if (url.protocol !== "https:" && !loopback) throw new Error("El servicio OAuth debe usar HTTPS.");
    this.options.baseUrl = url.toString().replace(/\/$/, "");
  }

  async signIn(signal?: AbortSignal): Promise<void> {
    const deviceId = await this.options.deviceStore.getOrCreate();
    const started = await this.publicJson("/v1/account-auth/start", {
      method: "POST",
      body: { device_id: deviceId, app_version: this.options.appVersion },
      signal
    });
    const authorizeUrl = safeAuthorizationUrl(stringValue(started.authorize_url));
    await this.options.openExternal(authorizeUrl);
    const attemptId = stringValue(started.attempt_id);
    const expiresInSeconds = numberValue(started.expires_in);
    const maxAttempts = expiresInSeconds > 0
      ? Math.max(1, Math.ceil(expiresInSeconds * 1_000 / OAUTH_POLL_MS))
      : ACCOUNT_AUTH_ATTEMPTS;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const result = await this.publicJson("/v1/account-auth/status", {
        method: "POST",
        body: { attempt_id: attemptId, device_id: deviceId },
        signal
      });
      if (result.status === "complete" && isAccountIdentity(result.account)) {
        const session: AccountSession = {
          token: stringValue(result.token),
          refreshToken: stringValue(result.refresh_token),
          expiresAt: numberValue(result.expires_at),
          account: result.account
        };
        await this.options.accountStore.set(session);
        this.session = { token: session.token, expiresAt: session.expiresAt };
        return;
      }
      if (result.status === "error") throw new Error(stringValue(result.message) || "No fue posible iniciar sesión.");
      await delay(OAUTH_POLL_MS, signal);
    }
    throw new Error("El inicio de sesión expiró.");
  }

  async signOut(): Promise<void> {
    const stored = await this.options.accountStore.get();
    if (stored) {
      await this.publicJson("/v1/account-auth/logout", {
        method: "POST",
        headers: { Authorization: `Bearer ${stored.token}` }
      }).catch(() => {});
    }
    this.session = null;
    await this.options.accountStore.clear().catch((error) => {
      console.error(`[account-signout] No fue posible borrar la sesión local: ${oauthErrorMessage(error)}`);
    });
  }

  async deleteAccount(): Promise<void> {
    await this.authorizedJson("/v1/account/delete", {
      method: "POST",
      body: { confirmation: "DELETE" }
    });
    this.session = null;
    const cleanup = await Promise.allSettled([
      this.options.accountStore.clear(),
      this.options.deviceStore.clear()
    ]);
    for (const result of cleanup) {
      if (result.status === "rejected") {
        console.error(`[account-delete] No fue posible borrar un secreto local: ${oauthErrorMessage(result.reason)}`);
      }
    }
  }

  connector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson(`/v1/connectors/${encodeURIComponent(connectorId)}`, { signal });
  }

  connectors(signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors", { signal });
  }

  async loadAccountState(signal?: AbortSignal): Promise<AccountStateSnapshot> {
    return parseAccountStateSnapshot(await this.authorizedJson("/v1/account-state", { signal }));
  }

  async saveAccountState(
    state: AppState,
    baseRevision: number,
    signal?: AbortSignal
  ): Promise<AccountStateSnapshot> {
    try {
      return parseAccountStateSnapshot(await this.authorizedJson("/v1/account-state", {
        method: "POST",
        body: {
          base_revision: baseRevision,
          device_id: await this.options.deviceStore.getOrCreate(),
          state: normalizeAppState(state)
        },
        signal
      }));
    } catch (error) {
      if (error instanceof WrapperHttpError && error.status === 409 && isRecord(error.payload.current)) {
        throw new AccountStateConflictError(parseAccountStateSnapshot(error.payload.current));
      }
      throw error;
    }
  }

  startConnector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors/start", { method: "POST", body: { connector_id: connectorId }, signal });
  }

  connectorStatus(attemptId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors/status", {
      method: "POST",
      body: { attempt_id: attemptId },
      signal
    });
  }

  disconnectConnector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors/disconnect", { method: "POST", body: { connector_id: connectorId }, signal });
  }

  async warmAgent(botId: string, signal?: AbortSignal): Promise<void> {
    const result = await this.authorizedJson("/v1/agent/warm", {
      method: "POST",
      body: { bot_id: botId },
      signal
    });
    if (result.ready !== true) throw new Error("El agente todavía no está listo.");
  }

  runAgent(
    prompt: string,
    connectorIds: string[],
    options: {
      browser?: boolean;
      computer?: boolean;
      botId?: string;
      idempotencyKey?: string;
      executionMode?: "auto" | "agent" | "chat";
      chatPrompt?: string;
      userMessage?: string;
      signal?: AbortSignal;
      onDelta?: (text: string) => void;
    } = {}
  ): Promise<Record<string, unknown>> {
    return this.authorizedAgentStream({
      prompt,
      browser: options.browser === true,
      computer: options.computer === true,
      bot_id: options.botId ?? "",
      connector_ids: connectorIds,
      execution_mode: options.executionMode ?? "agent",
      chat_prompt: options.chatPrompt ?? "",
      user_message: options.userMessage ?? "",
      client_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      max_credits: 15,
      idempotency_key: options.idempotencyKey ?? randomUUID(),
      stream: true
    }, options.signal, options.onDelta);
  }

  async teachWorkflow(
    botName: string,
    frames: string[],
    durationMs: number,
    signal?: AbortSignal
  ): Promise<BotWorkflowDraft> {
    void botName;
    void frames;
    void durationMs;
    void signal;
    throw new Error("Teach a task está pausado mientras Agent Genia no tenga soporte visual.");
  }

  billing(signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/billing", { signal });
  }

  async computerStatus(botId: string, signal?: AbortSignal): Promise<BotComputerSnapshot> {
    return parseComputerSnapshot(await this.authorizedJson(
      `/v1/computers/${encodeURIComponent(botId)}`,
      { signal }
    ));
  }

  async ensureComputer(botId: string, botName: string, signal?: AbortSignal): Promise<BotComputerSnapshot> {
    return parseComputerSnapshot(await this.authorizedJson(
      `/v1/computers/${encodeURIComponent(botId)}/ensure`,
      { method: "POST", body: { bot_name: botName }, signal }
    ));
  }

  async handBackComputer(botId: string, signal?: AbortSignal): Promise<BotComputerSnapshot> {
    return parseComputerSnapshot(await this.authorizedJson(
      `/v1/computers/${encodeURIComponent(botId)}/hand-back`,
      { method: "POST", signal }
    ));
  }

  async deleteComputer(botId: string, signal?: AbortSignal): Promise<{ deleted: boolean }> {
    const result = await this.authorizedJson(
      `/v1/computers/${encodeURIComponent(botId)}/delete`,
      { method: "POST", signal }
    );
    return { deleted: result.deleted === true };
  }

  async startCheckout(tier: "basic" | "pro" | "business", signal?: AbortSignal): Promise<void> {
    const result = await this.authorizedJson("/v1/billing/checkout", {
      method: "POST",
      body: { tier },
      signal
    });
    await this.options.openExternal(safeStripeUrl(stringValue(result.checkout_url), "checkout.stripe.com"));
  }

  async openBillingPortal(signal?: AbortSignal): Promise<void> {
    const result = await this.authorizedJson("/v1/billing/portal", { method: "POST", signal });
    await this.options.openExternal(safeStripeUrl(stringValue(result.portal_url), "billing.stripe.com"));
  }

  async whatsAppStatus(signal?: AbortSignal): Promise<WhatsAppStatus> {
    return parseWhatsAppStatus(await this.authorizedJson("/v1/whatsapp/status", { signal }));
  }

  async startWhatsAppLink(signal?: AbortSignal): Promise<WhatsAppLinkStart> {
    const result = await this.authorizedJson("/v1/whatsapp/link", {
      method: "POST",
      signal
    });
    const status = parseWhatsAppStatus(result);
    const code = stringValue(result.code);
    const expiresAt = numberValue(result.expires_at);
    if (!code || expiresAt <= Date.now() / 1000) {
      throw new Error("El servicio devolvió un enlace de WhatsApp inválido.");
    }
    await this.options.openExternal(safeWhatsAppUrl(stringValue(result.url)));
    return { ...status, code, expiresAt };
  }

  async unlinkWhatsApp(signal?: AbortSignal): Promise<WhatsAppStatus> {
    await this.authorizedJson("/v1/whatsapp/unlink", { method: "POST", signal });
    return this.whatsAppStatus(signal);
  }

  private async authorizedJson(
    route: string,
    request: JsonRequestOptions = {},
    canRefresh = true
  ): Promise<Record<string, unknown>> {
    const session = await this.getSession(request.signal);
    try {
      return await this.publicJson(route, {
        ...request,
        headers: { ...request.headers, Authorization: `Bearer ${session.token}` }
      });
    } catch (error) {
      if (!(error instanceof WrapperHttpError) || error.status !== 401 || !canRefresh) throw error;
      await this.refreshSession(request.signal);
      return this.authorizedJson(route, request, false);
    }
  }

  private async getSession(signal?: AbortSignal): Promise<{ token: string; expiresAt: number }> {
    if (this.session && this.session.expiresAt - SESSION_REFRESH_SKEW_MS > Date.now()) return this.session;
    const stored = await this.options.accountStore.get();
    if (!stored) {
      const error = new Error("Primero inicia sesión en Agent Genia.");
      error.name = "AccountRequiredError";
      throw error;
    }
    if (stored.expiresAt - SESSION_REFRESH_SKEW_MS > Date.now()) {
      this.session = { token: stored.token, expiresAt: stored.expiresAt };
      return this.session;
    }
    return this.refreshSession(signal);
  }

  private async refreshSession(signal?: AbortSignal): Promise<{ token: string; expiresAt: number }> {
    if (!this.refreshPromise) {
      this.refreshPromise = this.refreshSessionOnce(signal).finally(() => { this.refreshPromise = null; });
    }
    return withSignal(this.refreshPromise, signal);
  }

  private async refreshSessionOnce(signal?: AbortSignal): Promise<{ token: string; expiresAt: number }> {
    const stored = await this.options.accountStore.get();
    if (!stored) {
      const error = new Error("Primero inicia sesión en Agent Genia.");
      error.name = "AccountRequiredError";
      throw error;
    }
    const deviceId = await this.options.deviceStore.getOrCreate();
    const refreshed = await this.publicJson("/v1/account-auth/refresh", {
      method: "POST",
      headers: { Authorization: `Bearer ${stored.refreshToken}` },
      body: { device_id: deviceId },
      signal
    });
    const next: AccountSession = {
      ...stored,
      token: stringValue(refreshed.token),
      refreshToken: stringValue(refreshed.refresh_token) || stored.refreshToken,
      expiresAt: numberValue(refreshed.expires_at)
    };
    await this.options.accountStore.set(next);
    this.session = { token: next.token, expiresAt: next.expiresAt };
    return this.session;
  }

  private async authorizedAgentStream(
    body: Record<string, unknown>,
    signal?: AbortSignal,
    onDelta?: (text: string) => void,
    canRefresh = true
  ): Promise<Record<string, unknown>> {
    const operationSignal = requestSignal(signal, AGENT_REQUEST_TIMEOUT_MS);
    const session = await this.getSession(operationSignal);
    throwIfAborted(operationSignal);
    let response: Response;
    try {
      response = await fetch(`${this.options.baseUrl}/v1/agent/run`, {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
          Authorization: `Bearer ${session.token}`
        },
        body: JSON.stringify(body),
        signal: operationSignal
      });
    } catch (error) {
      throw networkError(error, "No fue posible conectar con Agent Genia.");
    }
    if (response.status === 401 && canRefresh) {
      await this.refreshSession(operationSignal);
      return this.authorizedAgentStream(body, signal, onDelta, false);
    }
    if (!response.ok) {
      let payload: Record<string, unknown>;
      try {
        payload = await response.json() as Record<string, unknown>;
      } catch (error) {
        if (operationSignal.aborted) {
          throw networkError(operationSignal.reason ?? error, "No fue posible recibir la respuesta de Agent Genia.");
        }
        payload = {};
      }
      const nested = isRecord(payload.error) ? payload.error : {};
      throw new WrapperHttpError(
        typeof nested.message === "string" ? nested.message : `Agent Genia respondió HTTP ${response.status}.`,
        response.status,
        payload
      );
    }
    if (!response.headers.get("content-type")?.toLowerCase().includes("text/event-stream")) {
      try {
        return await response.json() as Record<string, unknown>;
      } catch (error) {
        if (operationSignal.aborted) {
          throw networkError(operationSignal.reason ?? error, "No fue posible recibir la respuesta de Agent Genia.");
        }
        throw error;
      }
    }
    if (!response.body) throw new Error("Agent Genia no devolvió un flujo de respuesta.");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamedText = "";
    let finalResponse: Record<string, unknown> | null = null;
    let runId = response.headers.get("x-agent-run-id")?.trim() || "";
    let streamFailure: unknown = null;

    const processFrame = (frame: string): void => {
      let eventName = "message";
      const data: string[] = [];
      for (const rawLine of frame.split(/\r?\n/)) {
        if (!rawLine || rawLine.startsWith(":")) continue;
        const separator = rawLine.indexOf(":");
        const field = separator < 0 ? rawLine : rawLine.slice(0, separator);
        const value = separator < 0 ? "" : rawLine.slice(separator + 1).replace(/^ /, "");
        if (field === "event") eventName = value;
        else if (field === "data") data.push(value);
      }
      const payload = data.join("\n");
      if (eventName === "start") {
        const parsed = JSON.parse(payload) as Record<string, unknown>;
        runId = stringValue(parsed.run_id) || runId;
      } else if (eventName === "delta") {
        const parsed = JSON.parse(payload) as Record<string, unknown>;
        const text = stringValue(parsed.text);
        if (text) {
          streamedText += text;
          onDelta?.(text);
        }
      } else if (eventName === "done64") {
        const answer = Buffer.from(payload.trim(), "base64").toString("utf8");
        if (!answer) throw new Error("Agent Genia devolvió una respuesta final inválida.");
        finalResponse = { answer };
      } else if (eventName === "done") {
        const parsed = JSON.parse(payload) as Record<string, unknown>;
        finalResponse = parsed;
      } else if (eventName === "error") {
        const parsed = JSON.parse(payload) as Record<string, unknown>;
        throw new WrapperHttpError(
          stringValue(parsed.message) || "Agent Genia no pudo completar la tarea.",
          numberValue(parsed.status) || 502,
          parsed
        );
      }
    };

    try {
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let boundary: RegExpMatchArray | null;
        while ((boundary = buffer.match(/\r?\n\r?\n/))) {
          const index = boundary.index ?? 0;
          const frame = buffer.slice(0, index);
          buffer = buffer.slice(index + boundary[0].length);
          if (frame.trim()) processFrame(frame);
        }
        if (done) break;
      }
      if (buffer.trim()) processFrame(buffer);
    } catch (error) {
      streamFailure = operationSignal.aborted
        ? networkError(operationSignal.reason ?? error, "La respuesta de Agent Genia tardó demasiado.")
        : error;
    } finally {
      reader.releaseLock();
    }
    if (finalResponse) return finalResponse;
    if (runId) {
      const recovered = await this.recoverAgentRun(runId, signal).catch(() => null);
      if (recovered) return recovered;
    }
    if (streamedText) return { answer: streamedText, run_id: runId };
    if (streamFailure) throw streamFailure;
    throw new Error("La ejecución no entregó una respuesta recuperable.");
  }

  private async recoverAgentRun(
    runId: string,
    signal?: AbortSignal
  ): Promise<Record<string, unknown> | null> {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const snapshot = await this.authorizedJson(
        `/v1/agent/runs/${encodeURIComponent(runId)}`,
        { signal }
      );
      if (snapshot.status === "succeeded" && isRecord(snapshot.result)) {
        return snapshot.result;
      }
      if (["failed", "cancelled", "budget_exhausted", "expired"].includes(stringValue(snapshot.status))) {
        throw new Error(`La ejecución terminó con estado ${stringValue(snapshot.status)}.`);
      }
      if (attempt < 119) await delay(500, signal);
    }
    return null;
  }

  private async publicJson(route: string, request: JsonRequestOptions = {}): Promise<Record<string, unknown>> {
    const signal = requestSignal(request.signal, JSON_REQUEST_TIMEOUT_MS);
    throwIfAborted(signal);
    let response: Response;
    try {
      response = await fetch(`${this.options.baseUrl}${route}`, {
        method: request.method ?? "GET",
        headers: {
          ...request.headers,
          ...(request.body ? { "Content-Type": "application/json" } : {})
        },
        body: request.body ? JSON.stringify(request.body) : undefined,
        signal
      });
    } catch (error) {
      throw networkError(error, "No fue posible conectar con Agent Genia.");
    }
    let payload: Record<string, unknown>;
    try {
      payload = await response.json() as Record<string, unknown>;
    } catch (error) {
      if (signal.aborted) throw networkError(signal.reason ?? error, "No fue posible recibir la respuesta de Agent Genia.");
      payload = {};
    }
    if (!response.ok) {
      const nested = isRecord(payload.error) ? payload.error : {};
      const message = typeof nested.message === "string" ? nested.message : `El servicio OAuth respondió HTTP ${response.status}.`;
      throw new WrapperHttpError(message, response.status, payload);
    }
    return payload;
  }
}

class DeviceIdentityStore {
  private readonly filePath: string;
  private pending: Promise<string> | null = null;

  constructor(private readonly options: { safeStorage: SafeStorage; userDataPath: string }) {
    this.filePath = path.join(options.userDataPath, "secrets", "agent-genia-device.bin");
  }

  async getOrCreate(): Promise<string> {
    if (this.pending) return this.pending;
    this.pending = this.loadOrCreate().finally(() => { this.pending = null; });
    return this.pending;
  }

  private async loadOrCreate(): Promise<string> {
    if (!await this.options.safeStorage.isAsyncEncryptionAvailable()) throw new Error("Desbloquea la sesión del sistema para iniciar sesión.");
    try {
      const existing = (await this.options.safeStorage.decryptStringAsync(await readFile(this.filePath))).result;
      if (/^[0-9a-f-]{36}$/i.test(existing)) return existing;
    } catch {}
    const identity = randomUUID();
    await mkdir(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
    await writeFile(this.filePath, await this.options.safeStorage.encryptStringAsync(identity), { mode: 0o600 });
    await chmod(this.filePath, 0o600);
    return identity;
  }

  async clear(): Promise<void> {
    await this.pending?.catch(() => undefined);
    await rm(this.filePath, { force: true });
  }
}

class EncryptedJsonStore<T> {
  constructor(private readonly options: {
    filePath: string;
    safeStorage: SafeStorage;
    validate: (value: unknown) => value is T;
  }) {}

  async get(): Promise<T | null> {
    try {
      const encrypted = await readFile(this.options.filePath);
      if (!await this.options.safeStorage.isAsyncEncryptionAvailable()) return null;
      const value: unknown = JSON.parse((await this.options.safeStorage.decryptStringAsync(encrypted)).result);
      return this.options.validate(value) ? value : null;
    } catch {
      return null;
    }
  }

  async set(value: T): Promise<void> {
    if (!this.options.validate(value)) throw new Error("El servicio OAuth devolvió una sesión inválida.");
    if (!await this.options.safeStorage.isAsyncEncryptionAvailable()) throw new Error("Desbloquea la sesión del sistema para guardar la cuenta.");
    await mkdir(path.dirname(this.options.filePath), { recursive: true, mode: 0o700 });
    const temporary = `${this.options.filePath}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`;
    try {
      await writeFile(temporary, await this.options.safeStorage.encryptStringAsync(JSON.stringify(value)), { mode: 0o600, flag: "wx" });
      await rename(temporary, this.options.filePath);
      await chmod(this.options.filePath, 0o600);
    } finally {
      await rm(temporary, { force: true }).catch(() => {});
    }
  }

  async clear(): Promise<void> {
    await rm(this.options.filePath, { force: true });
  }
}

function isAccountSession(value: unknown): value is AccountSession {
  return isRecord(value)
    && typeof value.token === "string"
    && typeof value.refreshToken === "string"
    && Number.isFinite(value.expiresAt)
    && isAccountIdentity(value.account);
}

function isAccountIdentity(value: unknown): value is AccountIdentity {
  return isRecord(value)
    && typeof value.id === "string"
    && value.id.startsWith("acct_")
    && typeof value.email === "string"
    && value.email.includes("@");
}

function isManagedConnectorPayload(value: unknown): value is ManagedConnectorPayload {
  return isRecord(value)
    && typeof value.managed_connection_id === "string"
    && value.managed_connection_id.length > 5
    && typeof value.connector_id === "string"
    && REMOTE_CONNECTOR_IDS.has(value.connector_id);
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function parseBillingSnapshot(value: Record<string, unknown>): BillingSnapshot {
  const tier = value.tier;
  const plans = isRecord(value.plans) ? value.plans : {};
  const basic = isRecord(plans.basic) ? plans.basic : {};
  const pro = isRecord(plans.pro) ? plans.pro : {};
  const business = isRecord(plans.business) ? plans.business : {};
  if (tier !== "free" && tier !== "basic" && tier !== "pro" && tier !== "business") {
    throw new Error("El servicio devolvió un plan inválido.");
  }
  const parsePlan = (
    plan: Record<string, unknown>,
    tierId: "basic" | "pro" | "business"
  ) => {
    const parsed = {
      name: stringValue(plan.name),
      amount: numberValue(plan.amount),
      currency: stringValue(plan.currency),
      interval: stringValue(plan.interval),
      five_hour_credits: numberValue(plan.five_hour_credits),
      seven_day_credits: numberValue(plan.seven_day_credits),
      monthly_credits: numberValue(plan.monthly_credits),
      max_concurrent_runs: numberValue(plan.max_concurrent_runs)
    };
    if (
      !parsed.name
      || parsed.amount <= 0
      || !parsed.currency
      || !parsed.interval
      || parsed.five_hour_credits <= 0
      || parsed.seven_day_credits <= 0
      || parsed.monthly_credits <= 0
      || parsed.max_concurrent_runs <= 0
    ) {
      throw new Error(`El servicio devolvió un catálogo incompleto para ${tierId}.`);
    }
    return parsed;
  };
  let subscription: BillingSnapshot["subscription"] = null;
  if (isRecord(value.subscription)) {
    const item = value.subscription;
    const itemTier = item.tier;
    if (itemTier === "basic" || itemTier === "pro" || itemTier === "business") {
      subscription = {
        stripe_subscription_id: stringValue(item.stripe_subscription_id),
        tier: itemTier,
        stripe_price_id: stringValue(item.stripe_price_id),
        status: stringValue(item.status),
        cancel_at_period_end: item.cancel_at_period_end === true,
        current_period_end: typeof item.current_period_end === "number" ? item.current_period_end : null
      };
    }
  }
  return {
    configured: value.configured === true,
    tier,
    customer: value.customer === true,
    subscription,
    plans: {
      basic: parsePlan(basic, "basic"),
      pro: parsePlan(pro, "pro"),
      business: parsePlan(business, "business")
    }
  };
}

function parseAccountStateSnapshot(value: Record<string, unknown>): AccountStateSnapshot {
  const revision = numberValue(value.revision);
  if (!Number.isSafeInteger(revision) || revision < 0 || !isRecord(value.state)) {
    throw new Error("El servicio devolvió un estado de cuenta inválido.");
  }
  return {
    revision,
    state: normalizeAppState(value.state),
    updatedAt: typeof value.updated_at === "number" && Number.isFinite(value.updated_at)
      ? value.updated_at
      : null
  };
}

function parseWhatsAppStatus(value: Record<string, unknown>): WhatsAppStatus {
  const activeBotId = value.active_bot_id;
  if (activeBotId !== null && activeBotId !== undefined && typeof activeBotId !== "string") {
    throw new Error("El servicio devolvió un enlace de WhatsApp inválido.");
  }
  return {
    configured: value.configured === true,
    connected: value.connected === true,
    displayName: stringValue(value.display_name),
    phoneHint: stringValue(value.phone_hint),
    activeBotId: typeof activeBotId === "string" && activeBotId ? activeBotId : null
  };
}

function parseComputerSnapshot(value: Record<string, unknown>): BotComputerSnapshot {
  const state = stringValue(value.state);
  if (!["disabled", "pulling", "running", "hibernated", "off", "error"].includes(state)) {
    throw new Error("El servicio devolvió un estado de computadora inválido.");
  }
  const viewerUrl = stringValue(value.viewer_url);
  if (viewerUrl) safeComputerViewerUrl(viewerUrl);
  return {
    configured: value.configured === true,
    bot_id: stringValue(value.bot_id),
    provider: stringValue(value.provider) || null,
    state: state as BotComputerSnapshot["state"],
    viewer_url: viewerUrl,
    viewer_expires_at: numberValue(value.viewer_expires_at),
    reason: stringValue(value.reason)
  };
}

export function safeComputerViewerUrl(value: string): string {
  const url = new URL(value);
  const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if ((url.protocol !== "https:" && !loopback) || url.username || url.password || url.href.length > 4096) {
    throw new Error("El servicio devolvió una URL de computadora insegura.");
  }
  return url.toString();
}

function safeAuthorizationUrl(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password) throw new Error("El servicio devolvió una URL OAuth insegura.");
  return url.toString();
}

function safeStripeUrl(value: string, expectedHost: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== expectedHost || url.username || url.password) {
    throw new Error("El servicio devolvió una URL de Stripe insegura.");
  }
  return url.toString();
}

function safeWhatsAppUrl(value: string): string {
  const url = new URL(value);
  if (
    url.protocol !== "https:"
    || url.hostname !== "wa.me"
    || url.username
    || url.password
    || !/^\/[0-9]{7,15}$/.test(url.pathname)
  ) {
    throw new Error("El servicio devolvió una URL de WhatsApp insegura.");
  }
  return url.toString();
}

function oauthErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function requestSignal(signal: AbortSignal | undefined, timeoutMs: number): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

function networkError(error: unknown, fallback: string): Error {
  if (error instanceof Error && error.name === "TimeoutError") {
    return new Error("La solicitud tardó demasiado. Revisa tu conexión e inténtalo de nuevo.");
  }
  if (error instanceof Error && error.name === "AbortError") {
    return new Error("La operación fue cancelada.");
  }
  if (error instanceof TypeError) return new Error(fallback);
  return error instanceof Error ? error : new Error(fallback);
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw signal.reason ?? new DOMException("La conexión fue cancelada.", "AbortError");
}

function shellSafeSignal(signal?: AbortSignal): AbortSignal | undefined {
  throwIfAborted(signal);
  return signal;
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const finish = () => { signal?.removeEventListener("abort", abort); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(signal?.reason ?? new DOMException("La conexión fue cancelada.", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    timer.unref?.();
  });
}

function withSignal<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  throwIfAborted(signal);
  return new Promise<T>((resolve, reject) => {
    const abort = () => {
      signal.removeEventListener("abort", abort);
      reject(signal.reason ?? new DOMException("La operación fue cancelada.", "AbortError"));
    };
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
