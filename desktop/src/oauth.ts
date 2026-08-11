import { randomBytes, randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import type { SafeStorage, Shell } from "electron";
import type {
  AccountConnectionStatus,
  ConnectorConnectionSnapshot,
  ConnectorConnectionStatus,
  OAuthProviderId
} from "./contracts";
import { CONNECTOR_CATALOG, MANAGED_CONNECTOR_IDS } from "./contracts";

const SESSION_REFRESH_SKEW_MS = 60_000;
const ACCOUNT_AUTH_ATTEMPTS = 120;
const CONNECTOR_AUTH_ATTEMPTS = 150;
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

interface ProviderSessionPayload {
  access_token: string;
  refresh_token?: string;
  expires_in?: number;
  account_label?: string;
  email?: string;
  instance_url?: string;
  api_domain?: string;
  accounts_server?: string;
}

interface ProviderSession extends ProviderSessionPayload {
  saved_at: number;
  owner_account_id: string;
}

interface ManagedConnectorSession {
  managed_connection_id: string;
  connector_id: string;
  account_label?: string;
  saved_at: number;
  owner_account_id: string;
}

interface JsonRequestOptions {
  method?: "GET" | "POST";
  headers?: Record<string, string>;
  body?: Record<string, unknown>;
  signal?: AbortSignal;
}

export const CONNECTOR_PROVIDER: Readonly<Record<string, OAuthProviderId | undefined>> = Object.freeze({
  "microsoft-365": "microsoft",
  hubspot: "hubspot",
  salesforce: "salesforce"
});

export const COMPOSIO_CONNECTOR_IDS: ReadonlySet<string> = new Set(MANAGED_CONNECTOR_IDS);

const PROVIDER_LABELS: Readonly<Record<OAuthProviderId, string>> = Object.freeze({
  google: "Google Workspace",
  microsoft: "Microsoft 365",
  hubspot: "HubSpot",
  salesforce: "Salesforce",
  pipedrive: "Pipedrive",
  zoho: "Zoho CRM",
  composio: "Composio"
});

export class DesktopOAuthController {
  private readonly accountStore: EncryptedJsonStore<AccountSession>;
  private readonly deviceStore: DeviceIdentityStore;
  private readonly client: OutcomeOAuthClient;
  private readonly providerSessions: Map<OAuthProviderId, ManagedProviderSession>;
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
    this.client = new OutcomeOAuthClient({
      baseUrl,
      accountStore: this.accountStore,
      deviceStore: this.deviceStore,
      appVersion,
      openExternal: (url) => shell.openExternal(url)
    });
    this.providerSessions = new Map(
      (Object.keys(PROVIDER_LABELS) as OAuthProviderId[]).map((provider) => [
        provider,
        new ManagedProviderSession({
          provider,
          displayName: PROVIDER_LABELS[provider],
          client: this.client,
          accountStore: this.accountStore,
          safeStorage,
          userDataPath,
          shell
        })
      ])
    );
    this.managedConnectorSessions = new Map(
      [...COMPOSIO_CONNECTOR_IDS].map((connectorId) => {
        const definition = CONNECTOR_CATALOG.find((item) => item.id === connectorId)!;
        return [
          connectorId,
          new ManagedConnectorAccount({
            connectorId,
            displayName: definition.name,
            client: this.client,
            accountStore: this.accountStore,
            safeStorage,
            userDataPath,
            shell
          })
        ];
      })
    );
  }

  async snapshot(): Promise<ConnectorConnectionSnapshot> {
    const account = await this.accountStatus();
    const connectors = await Promise.all(CONNECTOR_CATALOG.map(async (connector): Promise<ConnectorConnectionStatus> => {
      const provider = CONNECTOR_PROVIDER[connector.id];
      const managed = this.managedConnectorSessions.get(connector.id);
      if (managed) {
        const status = await managed.status();
        return {
          connectorId: connector.id,
          provider: "composio",
          available: true,
          connected: status.connected,
          account: status.account,
          reason: status.connected ? "" : account.connected ? "Listo para conectar tu cuenta real." : "Inicia sesión para conectar tu cuenta."
        };
      }
      if (!provider) {
        return {
          connectorId: connector.id,
          provider: null,
          available: false,
          connected: false,
          account: "",
          reason: "La app OAuth de este proveedor todavía no está registrada."
        };
      }
      const status = await this.providerSessions.get(provider)!.status();
      return {
        connectorId: connector.id,
        provider,
        available: true,
        connected: status.connected,
        account: status.account,
        reason: status.connected ? "" : account.connected ? "Listo para autorizar." : "Inicia sesión para conectar tu cuenta."
      };
    }));
    return { account, connectors };
  }

  async signIn(signal?: AbortSignal): Promise<ConnectorConnectionSnapshot> {
    await this.client.signIn(shellSafeSignal(signal));
    return this.snapshot();
  }

  async signOut(): Promise<ConnectorConnectionSnapshot> {
    await Promise.all([
      ...[...this.providerSessions.values()].map((session) => session.disconnect()),
      ...[...this.managedConnectorSessions.values()].map((session) => session.clearLocal())
    ]);
    await this.client.signOut();
    return this.snapshot();
  }

  async connect(connectorId: string, signal?: AbortSignal): Promise<ConnectorConnectionSnapshot> {
    const managed = this.managedConnectorSessions.get(connectorId);
    if (managed) {
      await managed.connect(shellSafeSignal(signal));
      return this.snapshot();
    }
    const provider = CONNECTOR_PROVIDER[connectorId];
    if (!provider) throw new Error("Este conector todavía no tiene una app OAuth pública configurada.");
    await this.providerSessions.get(provider)!.connect(shellSafeSignal(signal));
    return this.snapshot();
  }

  async disconnect(connectorId: string): Promise<ConnectorConnectionSnapshot> {
    const managed = this.managedConnectorSessions.get(connectorId);
    if (managed) {
      await managed.disconnect();
      return this.snapshot();
    }
    const provider = CONNECTOR_PROVIDER[connectorId];
    if (!provider) throw new Error("Este conector no admite desconexión OAuth.");
    await this.providerSessions.get(provider)!.disconnect();
    return this.snapshot();
  }

  private async accountStatus(): Promise<AccountConnectionStatus> {
    const stored = await this.accountStore.get();
    return stored
      ? { connected: true, required: true, email: stored.account.email, name: stored.account.name ?? "" }
      : { connected: false, required: true, email: "", name: "" };
  }
}

class ManagedConnectorAccount {
  private readonly store: EncryptedJsonStore<ManagedConnectorSession>;
  private connectPromise: Promise<void> | null = null;

  constructor(private readonly options: {
    connectorId: string;
    displayName: string;
    client: OutcomeOAuthClient;
    accountStore: EncryptedJsonStore<AccountSession>;
    safeStorage: SafeStorage;
    userDataPath: string;
    shell: Shell;
  }) {
    this.store = new EncryptedJsonStore({
      filePath: path.join(options.userDataPath, "secrets", `connector-managed-${options.connectorId}.bin`),
      safeStorage: options.safeStorage,
      validate: isManagedConnectorSession
    });
  }

  async status(): Promise<{ connected: boolean; account: string }> {
    const [stored, account] = await Promise.all([this.store.get(), this.options.accountStore.get()]);
    const connected = Boolean(stored?.managed_connection_id && account?.account.id && stored.owner_account_id === account.account.id);
    return { connected, account: connected ? stored?.account_label || account?.account.email || "" : "" };
  }

  async connect(signal?: AbortSignal): Promise<void> {
    if (!this.connectPromise) this.connectPromise = this.connectOnce(signal).finally(() => { this.connectPromise = null; });
    await withSignal(this.connectPromise, signal);
  }

  async disconnect(): Promise<void> {
    const account = await this.options.accountStore.get();
    if (account) await this.options.client.disconnectConnector(this.options.connectorId);
    await this.store.clear();
  }

  clearLocal(): Promise<void> {
    return this.store.clear();
  }

  private async connectOnce(signal?: AbortSignal): Promise<void> {
    let account = await this.options.accountStore.get();
    if (!account) {
      await this.options.client.signIn(signal);
      account = await this.options.accountStore.get();
    }
    if (!account) throw new Error("No se pudo verificar tu cuenta de Agent Genia.");
    const provider = await this.options.client.connector(this.options.connectorId, signal);
    if (provider.available !== true) throw new Error(stringValue(provider.reason) || `${this.options.displayName} todavía no está configurado.`);
    const started = await this.options.client.startConnector(this.options.connectorId, signal);
    await this.options.shell.openExternal(safeAuthorizationUrl(stringValue(started.authorize_url)));
    for (let attempt = 0; attempt < CONNECTOR_AUTH_ATTEMPTS; attempt += 1) {
      const status = await this.options.client.connectorStatus(stringValue(started.attempt_id), signal);
      if (status.status === "complete" && isManagedConnectorPayload(status.session)) {
        await this.store.set({
          ...status.session,
          saved_at: Date.now(),
          owner_account_id: account.account.id
        });
        return;
      }
      if (status.status === "error") throw new Error(stringValue(status.message) || `${this.options.displayName} rechazó la conexión.`);
      await delay(OAUTH_POLL_MS, signal);
    }
    throw new Error(`La conexión con ${this.options.displayName} expiró.`);
  }
}

class ManagedProviderSession {
  private readonly store: EncryptedJsonStore<ProviderSession>;
  private connectPromise: Promise<void> | null = null;

  constructor(private readonly options: {
    provider: OAuthProviderId;
    displayName: string;
    client: OutcomeOAuthClient;
    accountStore: EncryptedJsonStore<AccountSession>;
    safeStorage: SafeStorage;
    userDataPath: string;
    shell: Shell;
  }) {
    this.store = new EncryptedJsonStore({
      filePath: path.join(options.userDataPath, "secrets", `connector-${options.provider}.bin`),
      safeStorage: options.safeStorage,
      validate: isProviderSession
    });
  }

  async status(): Promise<{ connected: boolean; account: string }> {
    const [providerSession, accountSession] = await Promise.all([this.store.get(), this.options.accountStore.get()]);
    const connected = Boolean(
      providerSession?.access_token
      && accountSession?.account.id
      && providerSession.owner_account_id === accountSession.account.id
    );
    return {
      connected,
      account: connected ? providerSession?.account_label ?? providerSession?.email ?? accountSession?.account.email ?? "" : ""
    };
  }

  async connect(signal?: AbortSignal): Promise<void> {
    if (!this.connectPromise) {
      this.connectPromise = this.connectOnce(signal).finally(() => { this.connectPromise = null; });
    }
    await withSignal(this.connectPromise, signal);
  }

  async disconnect(): Promise<void> {
    await this.store.clear();
  }

  private async connectOnce(signal?: AbortSignal): Promise<void> {
    throwIfAborted(signal);
    let account = await this.options.accountStore.get();
    if (!account) {
      await this.options.client.signIn(signal);
      account = await this.options.accountStore.get();
    }
    if (!account) throw new Error("No se pudo verificar tu cuenta de Agent Genia.");

    const providerInfo = await this.options.client.provider(this.options.provider, signal);
    if (!providerInfo.configured) throw new Error(`${this.options.displayName} todavía no está configurado en producción.`);
    const started = await this.options.client.startProvider(this.options.provider, signal);
    await this.options.shell.openExternal(safeAuthorizationUrl(stringValue(started.authorize_url)));

    for (let attempt = 0; attempt < CONNECTOR_AUTH_ATTEMPTS; attempt += 1) {
      const status = await this.options.client.providerStatus(stringValue(started.attempt_id), signal);
      if (status.status === "complete" && isProviderSessionPayload(status.session)) {
        await this.store.set({
          ...status.session,
          saved_at: Date.now(),
          owner_account_id: account.account.id
        });
        return;
      }
      if (status.status === "error") throw new Error(stringValue(status.message) || `${this.options.displayName} rechazó la conexión.`);
      await delay(OAUTH_POLL_MS, signal);
    }
    throw new Error(`La conexión con ${this.options.displayName} expiró.`);
  }
}

class OutcomeOAuthClient {
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
      const result = await this.publicJson(`/v1/account-auth/status/${encodeURIComponent(attemptId)}?device_id=${encodeURIComponent(deviceId)}`, { signal });
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
    this.session = null;
    await this.options.accountStore.clear();
  }

  provider(provider: OAuthProviderId, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson(`/v1/oauth/providers/${encodeURIComponent(provider)}`, { signal });
  }

  startProvider(provider: OAuthProviderId, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/oauth/start", { method: "POST", body: { provider }, signal });
  }

  providerStatus(attemptId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson(`/v1/oauth/status/${encodeURIComponent(attemptId)}`, { signal });
  }

  connector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson(`/v1/connectors/${encodeURIComponent(connectorId)}`, { signal });
  }

  startConnector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors/start", { method: "POST", body: { connector_id: connectorId }, signal });
  }

  connectorStatus(attemptId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson(`/v1/connectors/status/${encodeURIComponent(attemptId)}`, { signal });
  }

  disconnectConnector(connectorId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.authorizedJson("/v1/connectors/disconnect", { method: "POST", body: { connector_id: connectorId }, signal });
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
}

class EncryptedJsonStore<T> {
  constructor(private readonly options: {
    filePath: string;
    safeStorage: SafeStorage;
    validate: (value: unknown) => value is T;
  }) {}

  async get(): Promise<T | null> {
    if (!this.options.safeStorage.isEncryptionAvailable()) return null;
    try {
      const value: unknown = JSON.parse(this.options.safeStorage.decryptString(await readFile(this.options.filePath)));
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

function isProviderSession(value: unknown): value is ProviderSession {
  if (!isRecord(value) || !isProviderSessionPayload(value)) return false;
  const persisted = value as ProviderSessionPayload & Record<string, unknown>;
  return typeof persisted.saved_at === "number"
    && Number.isFinite(persisted.saved_at)
    && typeof persisted.owner_account_id === "string"
    && persisted.owner_account_id.startsWith("acct_");
}

function isProviderSessionPayload(value: unknown): value is ProviderSessionPayload {
  return isRecord(value) && typeof value.access_token === "string" && value.access_token.length > 10;
}

function isManagedConnectorPayload(value: unknown): value is Pick<ManagedConnectorSession, "managed_connection_id" | "connector_id" | "account_label"> {
  return isRecord(value)
    && typeof value.managed_connection_id === "string"
    && value.managed_connection_id.length > 5
    && typeof value.connector_id === "string"
    && COMPOSIO_CONNECTOR_IDS.has(value.connector_id);
}

function isManagedConnectorSession(value: unknown): value is ManagedConnectorSession {
  if (!isManagedConnectorPayload(value)) return false;
  const persisted = value as typeof value & Record<string, unknown>;
  return typeof persisted.saved_at === "number"
    && Number.isFinite(persisted.saved_at)
    && typeof persisted.owner_account_id === "string"
    && persisted.owner_account_id.startsWith("acct_");
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

function safeAuthorizationUrl(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password) throw new Error("El servicio devolvió una URL OAuth insegura.");
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
