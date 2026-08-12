import Foundation
import Security

enum AppEnvironment {
    static let baseURL: URL = {
        let processValue = ProcessInfo.processInfo.environment["AGENTGENIA_API_BASE_URL"]
        let bundleValue = Bundle.main.object(forInfoDictionaryKey: "AgentGeniaAPIBaseURL") as? String
        let value = processValue ?? bundleValue ?? "https://agentgenia-api.onrender.com"
        guard let url = URL(string: value),
              (url.scheme == "https" || (url.scheme == "http" && ["localhost", "127.0.0.1", "::1"].contains(url.host)))
        else { preconditionFailure("AgentGeniaAPIBaseURL debe ser HTTPS o loopback") }
        return url
    }()

    #if DEBUG
    static let externalBillingEnabled = true
    #else
    static let externalBillingEnabled = false
    #endif
}

struct ServiceError: LocalizedError, Sendable {
    let message: String
    let code: String
    let status: Int

    var errorDescription: String? { message }
}

private struct ErrorEnvelope: Decodable {
    struct Detail: Decodable { let message: String?; let type: String? }
    let error: Detail?
}

private struct EmptyBody: Encodable, Sendable {}
private struct EmptyResponse: Decodable, Sendable {}

private struct AuthStartRequest: Encodable, Sendable {
    let deviceID: String
    let appVersion: String
    enum CodingKeys: String, CodingKey { case deviceID = "device_id"; case appVersion = "app_version" }
}

struct AuthStartResponse: Decodable, Sendable {
    let attemptID: String
    let authorizeURL: String
    let expiresIn: Int
    enum CodingKeys: String, CodingKey {
        case attemptID = "attempt_id"; case authorizeURL = "authorize_url"; case expiresIn = "expires_in"
    }
}

struct AuthStatusResponse: Decodable, Sendable {
    let status: String
    let message: String?
    let token: String?
    let refreshToken: String?
    let expiresAt: Int64?
    let account: AccountIdentity?
    enum CodingKeys: String, CodingKey {
        case status, message, token, account
        case refreshToken = "refresh_token"; case expiresAt = "expires_at"
    }
}

private struct RefreshRequest: Encodable, Sendable {
    let deviceID: String
    enum CodingKeys: String, CodingKey { case deviceID = "device_id" }
}

private struct AuthStatusRequest: Encodable, Sendable {
    let attemptID: String
    let deviceID: String
    enum CodingKeys: String, CodingKey { case attemptID = "attempt_id"; case deviceID = "device_id" }
}

private struct AttemptStatusRequest: Encodable, Sendable {
    let attemptID: String
    enum CodingKeys: String, CodingKey { case attemptID = "attempt_id" }
}

private struct ConnectorStartRequest: Encodable, Sendable {
    let connectorID: String
    enum CodingKeys: String, CodingKey { case connectorID = "connector_id" }
}

struct ConnectorStartResponse: Decodable, Sendable {
    let attemptID: String
    let authorizeURL: String
    enum CodingKeys: String, CodingKey { case attemptID = "attempt_id"; case authorizeURL = "authorize_url" }
}

struct ConnectorPollResponse: Decodable, Sendable {
    let status: String
    let message: String?
}

private struct AgentRunRequest: Encodable, Sendable {
    let prompt: String
    let browser: Bool
    let computer: Bool
    let botID: String
    let connectorIDs: [String]
    enum CodingKeys: String, CodingKey {
        case prompt, browser, computer
        case botID = "bot_id"; case connectorIDs = "connector_ids"
    }
}

struct AgentRunResponse: Decodable, Sendable { let answer: String }

private struct ComputerEnsureRequest: Encodable, Sendable {
    let botName: String
    enum CodingKeys: String, CodingKey { case botName = "bot_name" }
}

private struct CheckoutRequest: Encodable, Sendable { let tier: String }
private struct CheckoutResponse: Decodable, Sendable {
    let checkoutURL: String
    enum CodingKeys: String, CodingKey { case checkoutURL = "checkout_url" }
}
private struct PortalResponse: Decodable, Sendable {
    let portalURL: String
    enum CodingKeys: String, CodingKey { case portalURL = "portal_url" }
}

actor APIClient {
    private let baseURL: URL
    private let urlSession: URLSession
    private let keychain = KeychainSessionStore()
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private var session: AccountSession?
    private var refreshTask: Task<AccountSession, Error>?

    init(baseURL: URL) {
        self.baseURL = baseURL
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.httpMaximumConnectionsPerHost = 6
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 1_800
        self.urlSession = URLSession(configuration: configuration)
    }

    func restoreSession() throws -> AccountSession? {
        if session == nil { session = try keychain.read() }
        return session
    }

    func beginSignIn() async throws -> AuthStartResponse {
        try await request(
            "/v1/account-auth/start",
            method: "POST",
            body: AuthStartRequest(deviceID: deviceID, appVersion: appVersion),
            authorized: false
        )
    }

    func authStatus(attemptID: String) async throws -> AuthStatusResponse {
        return try await request(
            "/v1/account-auth/status",
            method: "POST",
            body: AuthStatusRequest(attemptID: attemptID, deviceID: deviceID),
            authorized: false
        )
    }

    func saveCompletedSignIn(_ response: AuthStatusResponse) throws -> AccountSession {
        guard let token = response.token,
              let refreshToken = response.refreshToken,
              let expiresAt = response.expiresAt,
              let account = response.account
        else { throw ServiceError(message: "El servidor devolvió una sesión incompleta.", code: "invalid_session", status: 500) }
        let next = AccountSession(token: token, refreshToken: refreshToken, expiresAt: expiresAt, account: account)
        try keychain.write(next)
        session = next
        return next
    }

    func signOut() async {
        refreshTask?.cancel()
        refreshTask = nil
        if let current = session ?? (try? keychain.read()) {
            _ = try? await rawRequest(
                "/v1/account-auth/logout",
                method: "POST",
                bodyData: nil,
                authorization: current.token,
                canRefresh: false
            )
        }
        session = nil
        try? keychain.clear()
    }

    func clearLocalSessionForUITesting() {
        refreshTask?.cancel()
        refreshTask = nil
        session = nil
        try? keychain.clear()
    }

    func me() async throws -> AccountProfile {
        try await request("/v1/me", body: Optional<EmptyBody>.none)
    }

    func connectors() async throws -> ConnectorSnapshot {
        try await request("/v1/connectors", body: Optional<EmptyBody>.none)
    }

    func startConnector(_ connectorID: String) async throws -> ConnectorStartResponse {
        try await request(
            "/v1/connectors/start",
            method: "POST",
            body: ConnectorStartRequest(connectorID: connectorID)
        )
    }

    func connectorStatus(_ attemptID: String) async throws -> ConnectorPollResponse {
        try await request(
            "/v1/connectors/status",
            method: "POST",
            body: AttemptStatusRequest(attemptID: attemptID)
        )
    }

    func disconnectConnector(_ connectorID: String) async throws {
        let _: EmptyResponse = try await request(
            "/v1/connectors/disconnect",
            method: "POST",
            body: ConnectorStartRequest(connectorID: connectorID)
        )
    }

    func runAgent(
        prompt: String,
        botID: UUID,
        connectorIDs: [String],
        computer: Bool = true
    ) async throws -> AgentRunResponse {
        try await request(
            "/v1/agent/run",
            method: "POST",
            body: AgentRunRequest(
                prompt: prompt,
                browser: false,
                computer: computer,
                botID: botID.uuidString.lowercased(),
                connectorIDs: connectorIDs
            )
        )
    }

    func computerStatus(botID: UUID) async throws -> ComputerSnapshot {
        try await request(
            "/v1/computers/\(botID.uuidString.lowercased())",
            body: Optional<EmptyBody>.none
        )
    }

    func ensureComputer(botID: UUID, botName: String) async throws -> ComputerSnapshot {
        try await request(
            "/v1/computers/\(botID.uuidString.lowercased())/ensure",
            method: "POST",
            body: ComputerEnsureRequest(botName: botName)
        )
    }

    func handBackComputer(botID: UUID) async throws -> ComputerSnapshot {
        try await request(
            "/v1/computers/\(botID.uuidString.lowercased())/hand-back",
            method: "POST",
            body: EmptyBody()
        )
    }

    func billing() async throws -> BillingSnapshot {
        try await request("/v1/billing", body: Optional<EmptyBody>.none)
    }

    func checkoutURL(tier: String) async throws -> URL {
        let response: CheckoutResponse = try await request(
            "/v1/billing/checkout", method: "POST", body: CheckoutRequest(tier: tier)
        )
        return try safeURL(response.checkoutURL, hosts: ["checkout.stripe.com"])
    }

    func portalURL() async throws -> URL {
        let response: PortalResponse = try await request(
            "/v1/billing/portal", method: "POST", body: EmptyBody()
        )
        return try safeURL(response.portalURL, hosts: ["billing.stripe.com"])
    }

    private func request<Response: Decodable & Sendable, Body: Encodable & Sendable>(
        _ path: String,
        method: String = "GET",
        body: Body?,
        authorized: Bool = true
    ) async throws -> Response {
        let bodyData = try body.map { try encoder.encode($0) }
        let data = try await rawRequest(
            path,
            method: method,
            bodyData: bodyData,
            authorization: authorized ? try await accessToken() : nil,
            canRefresh: authorized
        )
        if Response.self == EmptyResponse.self, data.isEmpty || data == Data("{}".utf8) {
            return EmptyResponse() as! Response
        }
        do { return try decoder.decode(Response.self, from: data) }
        catch {
            throw ServiceError(message: "El servidor devolvió una respuesta incompatible.", code: "invalid_response", status: 502)
        }
    }

    private func rawRequest(
        _ path: String,
        method: String,
        bodyData: Data?,
        authorization: String?,
        canRefresh: Bool
    ) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw ServiceError(message: "Ruta inválida.", code: "invalid_url", status: 500)
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = path == "/v1/agent/run" ? 1_800 : 60
        request.httpBody = bodyData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if bodyData != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let authorization { request.setValue("Bearer \(authorization)", forHTTPHeaderField: "Authorization") }

        let (data, rawResponse): (Data, URLResponse)
        do { (data, rawResponse) = try await urlSession.data(for: request) }
        catch { throw ServiceError(message: "No fue posible conectar con Agent Genia.", code: "network_error", status: 0) }
        guard let response = rawResponse as? HTTPURLResponse else {
            throw ServiceError(message: "Respuesta de red inválida.", code: "network_error", status: 0)
        }
        if response.statusCode == 401 && canRefresh {
            let refreshed = try await refreshSession()
            return try await rawRequest(
                path,
                method: method,
                bodyData: bodyData,
                authorization: refreshed.token,
                canRefresh: false
            )
        }
        guard (200..<300).contains(response.statusCode) else {
            let envelope = try? decoder.decode(ErrorEnvelope.self, from: data)
            throw ServiceError(
                message: envelope?.error?.message ?? "Agent Genia respondió HTTP \(response.statusCode).",
                code: envelope?.error?.type ?? "http_error",
                status: response.statusCode
            )
        }
        return data
    }

    private func accessToken() async throws -> String {
        let current: AccountSession?
        if let session { current = session } else { current = try keychain.read() }
        guard let current else {
            throw ServiceError(message: "Primero inicia sesión en Agent Genia.", code: "account_required", status: 401)
        }
        session = current
        if current.expiresAt - 60_000 > Int64(Date().timeIntervalSince1970 * 1000) { return current.token }
        return try await refreshSession().token
    }

    private func refreshSession() async throws -> AccountSession {
        if let refreshTask { return try await refreshTask.value }
        let task = Task { try await performSessionRefresh() }
        refreshTask = task
        defer { refreshTask = nil }
        return try await task.value
    }

    private func performSessionRefresh() async throws -> AccountSession {
        let stored: AccountSession?
        if let session { stored = session } else { stored = try keychain.read() }
        guard let current = stored else {
            throw ServiceError(message: "Tu sesión expiró. Inicia sesión nuevamente.", code: "account_required", status: 401)
        }
        let data = try await rawRequest(
            "/v1/account-auth/refresh",
            method: "POST",
            bodyData: try encoder.encode(RefreshRequest(deviceID: deviceID)),
            authorization: current.refreshToken,
            canRefresh: false
        )
        do {
            let refreshed = try decoder.decode(AccountSession.self, from: data)
            try keychain.write(refreshed)
            session = refreshed
            return refreshed
        } catch {
            session = nil
            try? keychain.clear()
            throw ServiceError(message: "Tu sesión expiró. Inicia sesión nuevamente.", code: "account_required", status: 401)
        }
    }

    private var deviceID: String {
        let key = "agentgenia.device-id"
        if let existing = UserDefaults.standard.string(forKey: key), UUID(uuidString: existing) != nil { return existing }
        let created = UUID().uuidString.lowercased()
        UserDefaults.standard.set(created, forKey: key)
        return created
    }

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0"
    }

    private func pathComponent(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }

    private func safeURL(_ value: String, hosts: Set<String>? = nil) throws -> URL {
        guard let url = URL(string: value), url.scheme == "https", url.user == nil, url.password == nil,
              hosts == nil || hosts?.contains(url.host ?? "") == true
        else { throw ServiceError(message: "El servidor devolvió una URL no segura.", code: "unsafe_url", status: 502) }
        return url
    }
}

private struct KeychainSessionStore: @unchecked Sendable {
    private let service = "com.agentgenia.ios.session"
    private let account = "current"

    func read() throws -> AccountSession? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw ServiceError(message: "No pudimos leer la sesión segura.", code: "keychain_error", status: 0)
        }
        return try JSONDecoder().decode(AccountSession.self, from: data)
    }

    func write(_ value: AccountSession) throws {
        let data = try JSONEncoder().encode(value)
        let lookup: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        let updateStatus = SecItemUpdate(lookup as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecItemNotFound {
            var insertion = lookup
            attributes.forEach { insertion[$0.key] = $0.value }
            guard SecItemAdd(insertion as CFDictionary, nil) == errSecSuccess else {
                throw ServiceError(message: "No pudimos guardar la sesión segura.", code: "keychain_error", status: 0)
            }
        } else if updateStatus != errSecSuccess {
            throw ServiceError(message: "No pudimos actualizar la sesión segura.", code: "keychain_error", status: 0)
        }
    }

    func clear() throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        let status = SecItemDelete(query as CFDictionary)
        if status != errSecSuccess && status != errSecItemNotFound {
            throw ServiceError(message: "No pudimos cerrar la sesión segura.", code: "keychain_error", status: 0)
        }
    }
}
