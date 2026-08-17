import Foundation
import OSLog
import Security

enum AppEnvironment {
    static let baseURL: URL = {
        let processValue = ProcessInfo.processInfo.environment["AGENTGENIA_API_BASE_URL"]
        let bundleValue = Bundle.main.object(forInfoDictionaryKey: "AgentGeniaAPIBaseURL") as? String
        let value = processValue ?? bundleValue ?? "https://agentgenia-api.onrender.com"
        guard let url = URL(string: value),
              (url.scheme == "https" || (url.scheme == "http" && ["localhost", "127.0.0.1", "::1"].contains(url.host))),
              url.path.isEmpty || url.path == "/"
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

private struct AppleAuthRequest: Encodable, Sendable {
    let identityToken: String
    let authorizationCode: String
    let nonce: String
    let deviceID: String
    let name: String?
    enum CodingKeys: String, CodingKey {
        case identityToken = "identity_token"
        case authorizationCode = "authorization_code"
        case nonce
        case deviceID = "device_id"
        case name
    }
}

private struct DeleteAccountRequest: Encodable, Sendable {
    let confirmation = "DELETE"
}

private struct DeleteAccountResponse: Decodable, Sendable { let deleted: Bool }
private struct WhatsAppUnlinkResponse: Decodable, Sendable { let disconnected: Bool }

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
    let executionMode: String
    let chatPrompt: String
    let userMessage: String
    let browser: Bool
    let computer: Bool
    let botID: String
    let connectorIDs: [String]
    let maxCredits: Int
    let idempotencyKey: String
    let stream: Bool
    enum CodingKeys: String, CodingKey {
        case prompt, browser, computer
        case executionMode = "execution_mode"; case chatPrompt = "chat_prompt"; case userMessage = "user_message"
        case botID = "bot_id"; case connectorIDs = "connector_ids"
        case maxCredits = "max_credits"; case idempotencyKey = "idempotency_key"
        case stream
    }
}

private struct AgentWarmRequest: Encodable, Sendable {
    let botID: String
    enum CodingKeys: String, CodingKey { case botID = "bot_id" }
}

private struct AgentWarmResponse: Decodable, Sendable {
    let ready: Bool
    let started: Bool
}

struct AgentRunResponse: Decodable, Sendable { let answer: String }
private struct AgentStreamDelta: Decodable, Sendable { let text: String }
private struct AgentStreamFailure: Decodable, Sendable {
    let status: Int
    let message: String
    let type: String
}

struct ServerSentEvent: Equatable, Sendable {
    let name: String
    let data: Data
}

struct ServerSentEventParser: Sendable {
    private var eventName = "message"
    private var dataLines: [String] = []

    mutating func consume(line: String) -> ServerSentEvent? {
        // AsyncLineSequence normally removes CRLF, but intermediaries are
        // allowed to leave the carriage return attached to the yielded line.
        let normalized = line.last == "\r" ? String(line.dropLast()) : line
        if normalized.isEmpty {
            return emitPendingEvent()
        }
        if normalized.hasPrefix(":") {
            return nil
        }
        if normalized.hasPrefix("event:") {
            let nextEventName = String(normalized.dropFirst(6))
                .trimmingCharacters(in: .whitespaces)
            // Some HTTP stacks do not yield empty lines from an incremental
            // response. Seeing the next event is therefore also an explicit
            // boundary for the pending one.
            if !dataLines.isEmpty {
                let pending = emitPendingEvent()
                eventName = nextEventName
                return pending
            }
            eventName = nextEventName
        } else if normalized.hasPrefix("data:") {
            var value = String(normalized.dropFirst(5))
            if value.first == " " { value.removeFirst() }
            dataLines.append(value)
        }
        return nil
    }

    /// AsyncLineSequence is allowed to finish without yielding a final empty
    /// line. Flush the last frame at EOF so a valid `event: done` is not lost
    /// when an HTTP proxy closes the response immediately after its payload.
    mutating func finish() -> ServerSentEvent? {
        emitPendingEvent()
    }

    private mutating func emitPendingEvent() -> ServerSentEvent? {
        guard !dataLines.isEmpty else {
            eventName = "message"
            return nil
        }
        let frame = ServerSentEvent(
            name: eventName,
            data: Data(dataLines.joined(separator: "\n").utf8)
        )
        eventName = "message"
        dataLines.removeAll(keepingCapacity: true)
        return frame
    }
}

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

private struct AccountStateSaveRequest: Encodable, Sendable {
    let baseRevision: Int
    let deviceID: String
    let state: PersistedAccountState
    enum CodingKeys: String, CodingKey {
        case baseRevision = "base_revision"
        case deviceID = "device_id"
        case state
    }
}

actor APIClient {
    private static let deviceIDKey = "agentgenia.device-id"
    private let baseURL: URL
    private let urlSession: URLSession
    private let keychain = KeychainSessionStore()
    private let encoder: JSONEncoder = {
        let value = JSONEncoder()
        value.dateEncodingStrategy = .iso8601
        return value
    }()
    private let decoder: JSONDecoder = {
        let value = JSONDecoder()
        value.dateDecodingStrategy = .iso8601
        return value
    }()
    private var session: AccountSession?
    private var refreshTask: Task<AccountSession, Error>?
    private let logger = Logger(subsystem: "com.agentgenia.ios", category: "network")

    init(baseURL: URL) {
        self.baseURL = baseURL
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        configuration.waitsForConnectivity = true
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

    func signInWithApple(
        identityToken: String,
        authorizationCode: String,
        nonce: String,
        name: String?
    ) async throws -> AccountSession {
        let next: AccountSession = try await request(
            "/v1/account-auth/apple",
            method: "POST",
            body: AppleAuthRequest(
                identityToken: identityToken,
                authorizationCode: authorizationCode,
                nonce: nonce,
                deviceID: deviceID,
                name: name
            ),
            authorized: false
        )
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

    func deleteAccount() async throws {
        let response: DeleteAccountResponse = try await request(
            "/v1/account/delete", method: "POST", body: DeleteAccountRequest()
        )
        guard response.deleted else {
            throw ServiceError(
                message: "El servidor no confirmó la eliminación.",
                code: "deletion_unconfirmed",
                status: 502
            )
        }
        refreshTask?.cancel()
        refreshTask = nil
        session = nil
        try keychain.clear()
        UserDefaults.standard.removeObject(forKey: Self.deviceIDKey)
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

    func accountState() async throws -> AccountStateSnapshot {
        try await request("/v1/account-state", body: Optional<EmptyBody>.none)
    }

    func saveAccountState(_ state: PersistedAccountState, baseRevision: Int) async throws -> AccountStateSnapshot {
        try await request(
            "/v1/account-state",
            method: "POST",
            body: AccountStateSaveRequest(baseRevision: baseRevision, deviceID: deviceID, state: state)
        )
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
        idempotencyKey: String,
        executionMode: String = "agent",
        chatPrompt: String = "",
        userMessage: String = "",
        computer: Bool = true,
        onDelta: @escaping @Sendable (String) async -> Void
    ) async throws -> AgentRunResponse {
        let request = AgentRunRequest(
            prompt: prompt,
            executionMode: executionMode,
            chatPrompt: chatPrompt,
            userMessage: userMessage,
            browser: false,
            computer: computer,
            botID: botID.uuidString.lowercased(),
            connectorIDs: connectorIDs,
            maxCredits: 15,
            idempotencyKey: idempotencyKey,
            stream: true
        )
        return try await streamAgent(
            bodyData: try encoder.encode(request),
            authorization: try await accessToken(),
            canRefresh: true,
            onDelta: onDelta
        )
    }

    func warmAgent(botID: UUID) async throws {
        let response: AgentWarmResponse
        do {
            response = try await request(
                "/v1/agent/warm",
                method: "POST",
                body: AgentWarmRequest(botID: botID.uuidString.lowercased())
            )
        } catch let service as ServiceError where service.status == 0 {
            // Warming is idempotent. Render can close the first connection
            // while waking an idle service, so retry it once instead of
            // leaving the first real message to pay the full cold start.
            try await Task.sleep(for: .milliseconds(250))
            response = try await request(
                "/v1/agent/warm",
                method: "POST",
                body: AgentWarmRequest(botID: botID.uuidString.lowercased())
            )
        }
        guard response.ready || response.started else {
            throw ServiceError(
                message: "El agente todavía no está listo.",
                code: "pi_warm_incomplete",
                status: 502
            )
        }
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

    func whatsAppStatus() async throws -> WhatsAppStatus {
        try await request("/v1/whatsapp/status", body: Optional<EmptyBody>.none)
    }

    func startWhatsAppLink() async throws -> (WhatsAppLinkStart, URL) {
        let response: WhatsAppLinkStart = try await request(
            "/v1/whatsapp/link", method: "POST", body: EmptyBody()
        )
        return (response, try safeURL(response.url, hosts: ["wa.me"]))
    }

    func unlinkWhatsApp() async throws -> WhatsAppStatus {
        let _: WhatsAppUnlinkResponse = try await request(
            "/v1/whatsapp/unlink", method: "POST", body: EmptyBody()
        )
        return try await whatsAppStatus()
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
        catch { throw networkError(error, path: path) }
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

    private func streamAgent(
        bodyData: Data,
        authorization: String,
        canRefresh: Bool,
        onDelta: @escaping @Sendable (String) async -> Void
    ) async throws -> AgentRunResponse {
        let path = "/v1/agent/run"
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw ServiceError(message: "Ruta inválida.", code: "invalid_url", status: 500)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 1_800
        request.httpBody = bodyData
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(authorization)", forHTTPHeaderField: "Authorization")

        let bytes: URLSession.AsyncBytes
        let rawResponse: URLResponse
        do { (bytes, rawResponse) = try await urlSession.bytes(for: request) }
        catch { throw networkError(error, path: path) }
        guard let response = rawResponse as? HTTPURLResponse else {
            throw ServiceError(message: "Respuesta de red inválida.", code: "network_error", status: 0)
        }
        if response.statusCode == 401 && canRefresh {
            let refreshed = try await refreshSession()
            return try await streamAgent(
                bodyData: bodyData,
                authorization: refreshed.token,
                canRefresh: false,
                onDelta: onDelta
            )
        }
        guard (200..<300).contains(response.statusCode) else {
            var data = Data()
            do { for try await byte in bytes { data.append(byte) } }
            catch { throw networkError(error, path: path) }
            let envelope = try? decoder.decode(ErrorEnvelope.self, from: data)
            throw ServiceError(
                message: envelope?.error?.message ?? "Agent Genia respondió HTTP \(response.statusCode).",
                code: envelope?.error?.type ?? "http_error",
                status: response.statusCode
            )
        }
        if response.value(forHTTPHeaderField: "Content-Type")?.lowercased().contains("text/event-stream") != true {
            // Keeps the mobile release compatible while the streaming backend
            // rolls out across instances. The old endpoint returns one JSON body.
            var data = Data()
            do { for try await byte in bytes { data.append(byte) } }
            catch { throw networkError(error, path: path) }
            do { return try decoder.decode(AgentRunResponse.self, from: data) }
            catch {
                throw ServiceError(
                    message: "Agent Genia devolvió una respuesta inválida.",
                    code: "invalid_response",
                    status: response.statusCode
                )
            }
        }

        var parser = ServerSentEventParser()
        var finalResponse: AgentRunResponse?
        var streamedText = ""
        var pendingDelta = ""
        var lastDeltaFlush = Date.distantPast

        func decode(_ event: ServerSentEvent) throws -> (delta: String?, response: AgentRunResponse?) {
            if event.name == "delta" {
                do { return (try decoder.decode(AgentStreamDelta.self, from: event.data).text, nil) }
                catch {
                    throw ServiceError(
                        message: "Agent Genia recibió un fragmento de respuesta inválido.",
                        code: "invalid_stream_delta",
                        status: response.statusCode
                    )
                }
            } else if event.name == "done64" {
                let encoded = String(decoding: event.data, as: UTF8.self)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard let decodedData = Data(base64Encoded: encoded),
                      let answer = String(data: decodedData, encoding: .utf8)
                else {
                    throw ServiceError(
                        message: "Agent Genia recibió una respuesta final inválida.",
                        code: "invalid_stream_response",
                        status: response.statusCode
                    )
                }
                return (nil, AgentRunResponse(answer: answer))
            } else if event.name == "done" {
                do { return (nil, try decoder.decode(AgentRunResponse.self, from: event.data)) }
                catch {
                    throw ServiceError(
                        message: "Agent Genia recibió una respuesta final inválida.",
                        code: "invalid_stream_response",
                        status: response.statusCode
                    )
                }
            } else if event.name == "error", let failure = try? decoder.decode(AgentStreamFailure.self, from: event.data) {
                throw ServiceError(message: failure.message, code: failure.type, status: failure.status)
            }
            return (nil, nil)
        }

        do {
            for try await line in bytes.lines {
                if let event = parser.consume(line: line) {
                    let decoded = try decode(event)
                    if let delta = decoded.delta {
                        streamedText += delta
                        pendingDelta += delta
                        if pendingDelta.count >= 24
                            || Date().timeIntervalSince(lastDeltaFlush) >= 0.04 {
                            let value = pendingDelta
                            pendingDelta = ""
                            lastDeltaFlush = Date()
                            await onDelta(value)
                        }
                    }
                    if let response = decoded.response {
                        finalResponse = response
                    }
                }
            }
            if let event = parser.finish() {
                let decoded = try decode(event)
                if let delta = decoded.delta {
                    streamedText += delta
                    pendingDelta += delta
                    if pendingDelta.count >= 24
                        || Date().timeIntervalSince(lastDeltaFlush) >= 0.04 {
                        let value = pendingDelta
                        pendingDelta = ""
                        lastDeltaFlush = Date()
                        await onDelta(value)
                    }
                }
                if let response = decoded.response {
                    finalResponse = response
                }
            }
        } catch let error as ServiceError {
            throw error
        } catch {
            throw networkError(error, path: path)
        }
        if !pendingDelta.isEmpty {
            let value = pendingDelta
            pendingDelta = ""
            await onDelta(value)
        }
        guard let finalResponse else {
            throw ServiceError(
                message: "La conexión terminó antes de recibir la respuesta final.",
                code: "incomplete_stream",
                status: 502
            )
        }
        return finalResponse
    }

    private func networkError(_ error: Error, path: String) -> ServiceError {
        let nsError = error as NSError
        let urlError = error as? URLError
        let code = urlError?.errorCode ?? nsError.code
        logger.error("Request failed path=\(path, privacy: .public) domain=\(nsError.domain, privacy: .public) code=\(code, privacy: .public) description=\(nsError.localizedDescription, privacy: .public)")
#if DEBUG
        print("[AgentGenia.Network] path=\(path) domain=\(nsError.domain) code=\(code) description=\(nsError.localizedDescription)")
#endif
        let message: String
        switch urlError?.code {
        case .cancelled:
            message = "iOS canceló la solicitud antes de terminar (código \(code))."
        case .timedOut:
            message = "Agent Genia tardó demasiado en responder (código \(code))."
        case .networkConnectionLost:
            message = "La conexión se interrumpió mientras Agent Genia respondía (código \(code))."
        case .notConnectedToInternet:
            message = "El iPhone no tiene conexión a internet (código \(code))."
        case .cannotFindHost, .dnsLookupFailed:
            message = "El iPhone no pudo resolver el servidor de Agent Genia (código \(code))."
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateHasUnknownRoot, .clientCertificateRejected:
            message = "iOS rechazó la conexión segura con Agent Genia (código \(code))."
        default:
            message = "No fue posible conectar con Agent Genia (código \(code))."
        }
        return ServiceError(message: message, code: "network_\(code)", status: 0)
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
        if let existing = UserDefaults.standard.string(forKey: Self.deviceIDKey), UUID(uuidString: existing) != nil { return existing }
        let created = UUID().uuidString.lowercased()
        UserDefaults.standard.set(created, forKey: Self.deviceIDKey)
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
