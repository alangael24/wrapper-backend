import { randomBytes, randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import type { SafeStorage, Shell } from "electron";
import type {
  AccountConnectionStatus,
  BillingSnapshot,
  BotComputerSnapshot,
  BotWorkflowDraft,
  ConnectorConnectionSnapshot,
  ConnectorConnectionStatus
} from "./contracts";
import { CONNECTOR_CATALOG } from "./contracts";

const SESSION_REFRESH_SKEW_MS = 60_000;
const ACCOUNT_AUTH_ATTEMPTS = 120;
const CONNECTOR_AUTH_ATTEMPTS = 300;
const OAUTH_POLL_MS = 2_000;

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

export const COMPOSIO_CONNECTOR_IDS: ReadonlySet<string> = new Set(
  CONNECTOR_CATALOG.map((connector) => connector.id)
);
export const REMOTE_CONNECTOR_IDS: ReadonlySet<string> = COMPOSIO_CONNECTOR_IDS;

export class DesktopOAuthController {
  private readonly accountStore: EncryptedJsonStore<AccountSession>;
  private readonly deviceStore: DeviceIdentityStore;
  private readonly client: WrapperServiceClient;
  private readonly managedConnectorSessions: Map<string, ManagedConnectorAccount>;

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
    const response = account.connected ? await this.client.connectors().catch(() => null) : null;
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
          ? ""
          : !account.connected
            ? "Inicia sesión para conectar tu cuenta."
            : typeof remote?.reason === "string" && remote.reason
              ? remote.reason
              : available ? "Listo para conectar tu cuenta real." : "Este conector todavía no está configurado."
      };
    });
    return { account, connectors };
  }

  async signIn(signal?: AbortSignal): Promise<ConnectorConnectionSnapshot> {
    await this.client.signIn(shellSafeSignal(signal));
    return this.snapshot();
  }

  async signOut(): Promise<ConnectorConnectionSnapshot> {
    await this.client.signOut();
    return this.snapshot();
  }

  async deleteAccount(): Promise<ConnectorConnectionSnapshot> {
    await this.client.deleteAccount();
    return this.snapshot();
  }

  async connect(connectorId: string, signal?: AbortSignal): Promise<ConnectorConnectionSnapshot> {
    if (!await this.accountStore.get()) {
      await this.client.signIn(shellSafeSignal(signal));
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

  startCheckout(tier: "basic" | "pro", signal?: AbortSignal): Promise<void> {
    return this.client.startCheckout(tier, signal);
  }

  openBillingPortal(signal?: AbortSignal): Promise<void> {
    return this.client.openBillingPortal(signal);
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
    options: { browser?: boolean; computer?: boolean; botId?: string; signal?: AbortSignal } = {}
  ): Promise<Record<string, unknown>> {
    return this.client.runAgent(prompt, connectorIds, options);
  }

  teachWorkflow(botName: string, frames: string[], durationMs: number, signal?: AbortSignal): Promise<BotWorkflowDraft> {
    return this.client.teachWorkflow(botName, frames, durationMs, signal);
  }

  async accountId(): Promise<string | null> {
    return (await this.accountStore.get())?.account.id ?? null;
  }

  private async accountStatus(): Promise<AccountConnectionStatus> {
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
    for (let attempt = 0; attempt < ACCOUNT_AUTH_ATTEMPTS; attempt += 1) {
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
    await this.options.accountStore.clear();
  }

  async deleteAccount(): Promise<void> {
    await this.authorizedJson("/v1/account/delete", {
      method: "POST",
      body: { confirmation: "DELETE" }
    });
    this.session = null;
    await this.options.accountStore.clear();
    await this.options.deviceStore.clear();
  }

  connector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson(`/v1/connectors/${encodeURIComponent(connectorId)}`, { signal });
  }

  connectors(signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors", { signal });
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

  runAgent(
    prompt: string,
    connectorIds: string[],
    options: { browser?: boolean; computer?: boolean; botId?: string; signal?: AbortSignal } = {}
  ): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/agent/run", {
      method: "POST",
      body: {
        prompt,
        browser: options.browser === true,
        computer: options.computer === true,
        bot_id: options.botId ?? "",
        connector_ids: connectorIds
      },
      signal: options.signal
    });
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

  async startCheckout(tier: "basic" | "pro", signal?: AbortSignal): Promise<void> {
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

  private async authorizedJson(route: string, request: JsonRequestOptions = {}): Promise<Record<string, unknown>> {
    const session = await this.getSession(request.signal);
    return this.publicJson(route, {
      ...request,
      headers: { ...request.headers, Authorization: `Bearer ${session.token}` }
    });
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

  private async publicJson(route: string, request: JsonRequestOptions = {}): Promise<Record<string, unknown>> {
    throwIfAborted(request.signal);
    const response = await fetch(`${this.options.baseUrl}${route}`, {
      method: request.method ?? "GET",
      headers: {
        ...request.headers,
        ...(request.body ? { "Content-Type": "application/json" } : {})
      },
      body: request.body ? JSON.stringify(request.body) : undefined,
      signal: request.signal
    });
    const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) {
      const nested = isRecord(payload.error) ? payload.error : {};
      const message = typeof nested.message === "string" ? nested.message : `El servicio OAuth respondió HTTP ${response.status}.`;
      throw new Error(message);
    }
    return payload;
  }
}

class DeviceIdentityStore {
  private readonly filePath: string;

  constructor(private readonly options: { safeStorage: SafeStorage; userDataPath: string }) {
    this.filePath = path.join(options.userDataPath, "secrets", "agent-genia-device.bin");
  }

  async getOrCreate(): Promise<string> {
    if (!this.options.safeStorage.isEncryptionAvailable()) throw new Error("Desbloquea la sesión del sistema para iniciar sesión.");
    try {
      const existing = this.options.safeStorage.decryptString(await readFile(this.filePath));
      if (/^[0-9a-f-]{36}$/i.test(existing)) return existing;
    } catch {}
    const identity = randomUUID();
    await mkdir(path.dirname(this.filePath), { recursive: true, mode: 0o700 });
    await writeFile(this.filePath, this.options.safeStorage.encryptString(identity), { mode: 0o600 });
    await chmod(this.filePath, 0o600);
    return identity;
  }

  async clear(): Promise<void> {
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
      if (!this.options.safeStorage.isEncryptionAvailable()) return null;
      const value: unknown = JSON.parse(this.options.safeStorage.decryptString(encrypted));
      return this.options.validate(value) ? value : null;
    } catch {
      return null;
    }
  }

  async set(value: T): Promise<void> {
    if (!this.options.validate(value)) throw new Error("El servicio OAuth devolvió una sesión inválida.");
    if (!this.options.safeStorage.isEncryptionAvailable()) throw new Error("Desbloquea la sesión del sistema para guardar la cuenta.");
    await mkdir(path.dirname(this.options.filePath), { recursive: true, mode: 0o700 });
    const temporary = `${this.options.filePath}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`;
    try {
      await writeFile(temporary, this.options.safeStorage.encryptString(JSON.stringify(value)), { mode: 0o600, flag: "wx" });
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
  if (tier !== "free" && tier !== "basic" && tier !== "pro") {
    throw new Error("El servicio devolvió un plan inválido.");
  }
  const parsePlan = (plan: Record<string, unknown>, fallbackName: string, fallbackAmount: number) => ({
    name: stringValue(plan.name) || fallbackName,
    amount: numberValue(plan.amount) || fallbackAmount,
    currency: stringValue(plan.currency) || "usd",
    interval: stringValue(plan.interval) || "month"
  });
  let subscription: BillingSnapshot["subscription"] = null;
  if (isRecord(value.subscription)) {
    const item = value.subscription;
    const itemTier = item.tier;
    if (itemTier === "basic" || itemTier === "pro") {
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
    plans: { basic: parsePlan(basic, "Plus", 50), pro: parsePlan(pro, "Pro", 200) }
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
  return Promise.race([
    promise,
    new Promise<T>((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }))
  ]);
}
