import CryptoKit
import Foundation
import Observation
import UIKit

@MainActor
@Observable
final class AppModel {
    enum Phase { case loading, signedOut, ready }

    var phase: Phase = .loading
    var account: AccountIdentity?
    var profile: AccountProfile?
    var bots: [BotProfile] = []
    private var deletedBotIDs: [UUID] = []
    var selectedConnectorIDs: [String] = []
    var connectorStatuses: [String: ConnectorStatus] = [:]
    var billing: BillingSnapshot?
    var destination: Destination?
    var browserRequest: BrowserRequest?
    var computer: ComputerSnapshot?
    var computerBotID: UUID?
    var searchText = ""
    var showOnlyConnected = false
    var alertMessage: String?
    var isBusy = false
    var runningBotIDs: Set<UUID> = []

    private let api: APIClient
    private let stateStore = AccountStateStore()
    private var accountPollingTask: Task<Void, Never>?
    private var connectorPollingTask: Task<Void, Never>?
    private var connectorRefreshTask: Task<ConnectorSnapshot, Error>?
    private var billingRefreshTask: Task<BillingSnapshot, Error>?
    private var agentWarmTasks: [UUID: Task<Bool, Never>] = [:]
    private var persistenceTask: Task<Void, Never>?
    private var persistenceRequested = false
    private var warmedBotUntil: [UUID: Date] = [:]
    private var lastConnectorRefresh = Date.distantPast
    private var lastBillingRefresh = Date.distantPast
    private var browserPurpose: BrowserRequest.Purpose?
    private var accountStateRevision = 0

    private static let refreshFreshness: TimeInterval = 15

    init(api: APIClient? = nil) {
        self.api = api ?? APIClient(baseURL: AppEnvironment.baseURL)
    }

    func bootstrap() async {
        guard phase == .loading else { return }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing") {
            await api.clearLocalSessionForUITesting()
            phase = .signedOut
            return
        }
        do {
            guard let session = try await api.restoreSession() else {
                phase = .signedOut
                return
            }
            try await activate(session: session)
        } catch {
            await api.signOut()
            phase = .signedOut
            report(error)
        }
    }

    func beginSignIn() async {
        guard !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            let started = try await api.beginSignIn()
            let url = try trustedHTTPSURL(started.authorizeURL, allowedHosts: ["accounts.google.com"])
            presentBrowser(url: url, purpose: .account)
            accountPollingTask?.cancel()
            accountPollingTask = Task { await pollSignIn(attemptID: started.attemptID) }
        } catch { report(error) }
    }

    func completeAppleSignIn(
        identityToken: String,
        authorizationCode: String,
        nonce: String,
        name: String?
    ) async {
        guard !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            let session = try await api.signInWithApple(
                identityToken: identityToken,
                authorizationCode: authorizationCode,
                nonce: nonce,
                name: name
            )
            try await activate(session: session)
        } catch { report(error) }
    }

    func signOut() async {
        accountPollingTask?.cancel()
        accountPollingTask = nil
        connectorPollingTask?.cancel()
        connectorPollingTask = nil
        connectorRefreshTask?.cancel()
        connectorRefreshTask = nil
        billingRefreshTask?.cancel()
        billingRefreshTask = nil
        for task in agentWarmTasks.values { task.cancel() }
        agentWarmTasks = [:]
        persistenceTask?.cancel()
        persistenceTask = nil
        persistenceRequested = false
        warmedBotUntil = [:]
        lastConnectorRefresh = .distantPast
        lastBillingRefresh = .distantPast
        isBusy = false
        await api.signOut()
        clearAccountState()
    }

    func deleteAccount() async {
        guard let accountID = account?.id, !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            try await api.deleteAccount()
            do {
                try await stateStore.delete(accountID: accountID)
            } catch {
                clearAccountState()
                report(error)
                return
            }
            clearAccountState()
        } catch { report(error) }
    }

    private func clearAccountState() {
        persistenceTask?.cancel()
        persistenceTask = nil
        persistenceRequested = false
        account = nil
        profile = nil
        bots = []
        deletedBotIDs = []
        selectedConnectorIDs = []
        connectorStatuses = [:]
        billing = nil
        destination = nil
        browserRequest = nil
        browserPurpose = nil
        computer = nil
        for task in agentWarmTasks.values { task.cancel() }
        agentWarmTasks = [:]
        warmedBotUntil = [:]
        phase = .signedOut
    }

    func createBot() async {
        guard account != nil else { return }
        guard bots.count < 100 else {
            alertMessage = "Puedes tener como máximo 100 bots. Elimina uno antes de crear otro."
            return
        }
        let index = bots.count
        let bot = BotProfile(
            id: UUID(),
            name: "Nuevo bot",
            title: "",
            description: "",
            color: botColors[index % botColors.count],
            shape: BotShape.allCases[index % BotShape.allCases.count],
            notificationsEnabled: true,
            connectorIDs: selectedConnectorIDs,
            messages: [],
            createdAt: Date()
        )
        bots.append(bot)
        deletedBotIDs.removeAll { $0 == bot.id }
        destination = .bot(bot.id)
        await persistLocalState(dirty: true)
        schedulePersist()
        await prepareBot(botID: bot.id)
    }

    func updateBot(
        id: UUID,
        name: String,
        title: String,
        description: String,
        color: String,
        shape: BotShape,
        notificationsEnabled: Bool
    ) async {
        guard let index = bots.firstIndex(where: { $0.id == id }) else { return }
        bots[index].name = clean(name, maximum: 60, fallback: "Nuevo bot")
        bots[index].title = clean(title, maximum: 120)
        bots[index].description = clean(description, maximum: 600)
        bots[index].color = botColors.contains(color) ? color : bots[index].color
        bots[index].shape = shape
        bots[index].notificationsEnabled = notificationsEnabled
        await persistLocalState(dirty: true)
        schedulePersist()
    }

    func sendInitialMessageIfNeeded(botID: UUID) async {
        guard profile?.tier != "free" else { return }
        guard let bot = bots.first(where: { $0.id == botID }), bot.messages.isEmpty else { return }
        await runAgent(botID: botID, userText: "", initial: true)
    }

    func sendMessage(botID: UUID, text: String) async {
        let value = clean(text, maximum: 20_000)
        guard !value.isEmpty else { return }
        await runAgent(botID: botID, userText: value, initial: false)
    }

    func prepareBot(botID: UUID) async {
        // A new bot used to prewarm and send its initial turn concurrently.
        // Both requests target the same isolated Pi session, so the first
        // message could race the warm-up and fail with pi_busy (or pay another
        // cold start).  Make readiness explicit before the first turn.
        await warmAgent(botID: botID)
        await sendInitialMessageIfNeeded(botID: botID)
    }

    private func warmAgent(botID: UUID) async {
        guard phase == .ready, account != nil else { return }
        if let expiresAt = warmedBotUntil[botID], expiresAt > Date() { return }
        let task: Task<Bool, Never>
        if let existing = agentWarmTasks[botID] {
            task = existing
        } else {
            task = Task { [api] in
                do {
                    try await api.warmAgent(botID: botID)
                    return true
                } catch {
                    return false
                }
            }
            agentWarmTasks[botID] = task
        }
        let ready = await task.value
        agentWarmTasks[botID] = nil
        if ready { warmedBotUntil[botID] = Date().addingTimeInterval(10 * 60) }
    }

    private func awaitWarmAgentIfInFlight(botID: UUID) async {
        // The bot screen starts prewarming in the background. Reuse that work
        // when it exists, but do not create a second HTTP request on Send: the
        // agent run can start the same persistent Pi session itself.
        guard let task = agentWarmTasks[botID] else { return }
        let ready = await task.value
        agentWarmTasks[botID] = nil
        if ready { warmedBotUntil[botID] = Date().addingTimeInterval(10 * 60) }
    }

    func refreshConnectors(force: Bool = false) async {
        if !force, Date().timeIntervalSince(lastConnectorRefresh) < Self.refreshFreshness { return }
        if let existing = connectorRefreshTask {
            _ = try? await existing.value
            return
        }
        let accountID = account?.id
        let task = Task { try await api.connectors() }
        connectorRefreshTask = task
        defer { connectorRefreshTask = nil }
        do {
            let snapshot = try await task.value
            guard phase == .ready, account?.id == accountID else { return }
            lastConnectorRefresh = Date()
            connectorStatuses = Dictionary(uniqueKeysWithValues: snapshot.connectors.map { ($0.connectorID, $0) })
            let knownIDs = Set(ConnectorDefinition.catalog.map(\.id))
            let connectedIDs = snapshot.connectors
                .filter { $0.connected && knownIDs.contains($0.connectorID) }
                .map(\.connectorID)
                .sorted()
            let installedIDs = Array(Set(selectedConnectorIDs).union(connectedIDs)).sorted()
            let changed = selectedConnectorIDs != installedIDs
            selectedConnectorIDs = installedIDs
            if changed {
                await persistLocalState(dirty: true)
                schedulePersist()
            }
        } catch is CancellationError {
            return
        } catch {
            guard phase == .ready else { return }
            report(error)
        }
    }

    func connect(_ connectorID: String) async {
        guard !isBusy else { return }
        isBusy = true
        do {
            let started = try await api.startConnector(connectorID)
            let url = try trustedHTTPSURL(started.authorizeURL)
            presentBrowser(url: url, purpose: .connector(connectorID))
            connectorPollingTask = Task { [weak self] in
                await self?.pollConnector(attemptID: started.attemptID, connectorID: connectorID)
            }
        } catch {
            isBusy = false
            report(error)
        }
    }

    func disconnect(_ connectorID: String) async {
        do {
            try await api.disconnectConnector(connectorID)
            selectedConnectorIDs.removeAll { $0 == connectorID }
            for index in bots.indices { bots[index].connectorIDs.removeAll { $0 == connectorID } }
            await persistLocalState(dirty: true)
            schedulePersist()
            await refreshConnectors(force: true)
        } catch { report(error) }
    }

    func refreshBilling(force: Bool = false) async {
        if !force, Date().timeIntervalSince(lastBillingRefresh) < Self.refreshFreshness { return }
        if let existing = billingRefreshTask {
            _ = try? await existing.value
            return
        }
        let accountID = account?.id
        let task = Task { try await api.billing() }
        billingRefreshTask = task
        defer { billingRefreshTask = nil }
        do {
            let snapshot = try await task.value
            guard phase == .ready, account?.id == accountID else { return }
            billing = snapshot
            lastBillingRefresh = Date()
        } catch is CancellationError {
            return
        } catch {
            guard phase == .ready else { return }
            report(error)
        }
    }

    func startCheckout(tier: String) async {
        do {
            let url = try await api.checkoutURL(tier: tier)
            presentBrowser(url: url, purpose: .billing)
        } catch { report(error) }
    }

    func openBillingPortal() async {
        do {
            let url = try await api.portalURL()
            presentBrowser(url: url, purpose: .billing)
        } catch { report(error) }
    }

    func loadComputer(botID: UUID) async {
        computerBotID = botID
        do { computer = try await api.computerStatus(botID: botID) }
        catch { report(error) }
    }

    func ensureComputer(botID: UUID) async {
        guard let bot = bots.first(where: { $0.id == botID }), !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do {
            let snapshot = try await api.ensureComputer(botID: botID, botName: bot.name)
            computer = snapshot
            if snapshot.state == .running, let url = URL(string: snapshot.viewerURL), url.scheme == "https" {
                presentBrowser(url: url, purpose: .computer)
            }
        } catch { report(error) }
    }

    func openComputer() {
        guard let snapshot = computer,
              snapshot.state == .running,
              let url = URL(string: snapshot.viewerURL),
              url.scheme == "https"
        else { return }
        presentBrowser(url: url, purpose: .computer)
    }

    func handBackComputer(botID: UUID) async {
        guard !isBusy else { return }
        isBusy = true
        defer { isBusy = false }
        do { computer = try await api.handBackComputer(botID: botID) }
        catch { report(error) }
    }

    func dismissBrowser() {
        let dismissedPurpose = browserPurpose
        browserPurpose = nil
        browserRequest = nil
        if case .connector = dismissedPurpose, connectorPollingTask != nil {
            connectorPollingTask?.cancel()
            connectorPollingTask = nil
            isBusy = false
        }
        if dismissedPurpose == .billing {
            Task { await refreshBilling(force: true) }
        }
    }

    private func pollSignIn(attemptID: String) async {
        defer { accountPollingTask = nil }
        for _ in 0..<120 {
            guard !Task.isCancelled else { return }
            do {
                let status = try await api.authStatus(attemptID: attemptID)
                if status.status == "complete" {
                    let session = try await api.saveCompletedSignIn(status)
                    browserRequest = nil
                    try await activate(session: session)
                    return
                }
                if status.status == "error" {
                    browserRequest = nil
                    throw ServiceError(message: status.message ?? "Google rechazó el inicio de sesión.", code: "auth_error", status: 400)
                }
            } catch {
                browserRequest = nil
                report(error)
                return
            }
            try? await Task.sleep(for: .seconds(2))
        }
        browserRequest = nil
        report(ServiceError(message: "El inicio de sesión expiró.", code: "auth_timeout", status: 408))
    }

    private func pollConnector(attemptID: String, connectorID: String) async {
        for _ in 0..<300 {
            guard !Task.isCancelled else { return }
            do {
                let status = try await api.connectorStatus(attemptID)
                if status.status == "complete" {
                    if !selectedConnectorIDs.contains(connectorID) { selectedConnectorIDs.append(connectorID) }
                    connectorPollingTask = nil
                    isBusy = false
                    browserRequest = nil
                    await persistLocalState(dirty: true)
                    schedulePersist()
                    await refreshConnectors(force: true)
                    return
                }
                if status.status == "error" {
                    connectorPollingTask = nil
                    isBusy = false
                    browserRequest = nil
                    throw ServiceError(message: status.message ?? "El proveedor rechazó la conexión.", code: "connector_error", status: 400)
                }
            } catch {
                guard !Task.isCancelled else { return }
                connectorPollingTask = nil
                isBusy = false
                browserRequest = nil
                report(error)
                return
            }
            do { try await Task.sleep(for: .seconds(2)) }
            catch { return }
        }
        connectorPollingTask = nil
        isBusy = false
        browserRequest = nil
        report(ServiceError(message: "La conexión expiró.", code: "connector_timeout", status: 408))
    }

    private func activate(session: AccountSession) async throws {
        account = session.account
        profile = nil
        let cache = try await stateStore.load(accountID: session.account.id)
        accountStateRevision = cache.serverRevision
        bots = cache.state.bots
        deletedBotIDs = cache.state.deletedBotIDs
        selectedConnectorIDs = cache.state.selectedConnectorIDs
        if let active = cache.state.activeBotID, bots.contains(where: { $0.id == active }) {
            destination = .bot(active)
        } else if let first = bots.first {
            destination = .bot(first.id)
        } else {
            destination = .plugins
        }
        phase = .ready
        Task { [weak self] in
            guard let self else { return }
            async let accountStateRefresh: Void = self.reconcileAccountState(
                accountID: session.account.id,
                cachedState: cache.state,
                cachedDirty: cache.dirty
            )
            async let profileRefresh: Void = self.refreshProfile(accountID: session.account.id)
            async let connectorRefresh: Void = self.refreshConnectors()
            async let billingRefresh: Void = self.refreshBilling()
            _ = await (accountStateRefresh, profileRefresh, connectorRefresh, billingRefresh)
        }
    }

    private func reconcileAccountState(
        accountID: String,
        cachedState: PersistedAccountState,
        cachedDirty: Bool
    ) async {
        do {
            let remote = try await api.accountState()
            guard phase == .ready, account?.id == accountID else { return }
            let live = currentState()
            let hasLocalChanges = cachedDirty || live != cachedState
            var resolved = hasLocalChanges
                ? mergeAccountStates(remote.state, live)
                : remote.state
            var revision = remote.revision
            if hasLocalChanges, resolved != remote.state {
                do {
                    let saved = try await api.saveAccountState(
                        resolved, baseRevision: remote.revision
                    )
                    resolved = saved.state
                    revision = saved.revision
                } catch let service as ServiceError where service.status == 409 {
                    let latest = try await api.accountState()
                    resolved = mergeAccountStates(latest.state, currentState())
                    let saved = try await api.saveAccountState(
                        resolved, baseRevision: latest.revision
                    )
                    resolved = saved.state
                    revision = saved.revision
                }
            }
            guard !Task.isCancelled, phase == .ready, account?.id == accountID else { return }
            // If another local mutation landed while reconciliation was in
            // flight, keep it and let the serialized writer publish it next.
            let latestLocal = currentState()
            if latestLocal != live {
                resolved = mergeAccountStates(resolved, latestLocal)
                persistenceRequested = true
            }
            let currentDestination = destination
            applyRemoteState(resolved)
            switch currentDestination {
            case let .some(.bot(id)) where bots.contains(where: { $0.id == id }):
                destination = .bot(id)
            case .some(.plugins):
                destination = .plugins
            case .some(.account):
                destination = .account
            default:
                break
            }
            accountStateRevision = revision
            try await stateStore.save(
                currentState(), accountID: accountID,
                serverRevision: revision, dirty: persistenceRequested
            )
            if persistenceRequested { schedulePersist() }
        } catch is CancellationError {
            return
        } catch {
            guard phase == .ready, account?.id == accountID else { return }
            // A free-tier host may be waking up. Keep local state available and
            // use the conflict-aware writer as the retry path.
            await persistLocalState(dirty: true)
            schedulePersist()
        }
    }

    private func refreshProfile(accountID: String) async {
        do {
            let nextProfile = try await api.me()
            guard phase == .ready, account?.id == accountID else { return }
            profile = nextProfile
        } catch {
            guard phase == .ready, account?.id == accountID else { return }
            if let serviceError = error as? ServiceError, serviceError.status == 401 {
                await signOut()
            } else {
                report(error)
            }
        }
    }

    private func runAgent(botID: UUID, userText: String, initial: Bool) async {
        guard !runningBotIDs.contains(botID), let source = bots.first(where: { $0.id == botID }) else { return }
        // Give an in-flight response time to finish when the user briefly
        // backgrounds the app.  A regular foreground URLSession may otherwise
        // be suspended immediately and surface a misleading connection error.
        let backgroundTask = UIApplication.shared.beginBackgroundTask(
            withName: "AgentGeniaAgentRun"
        )
        defer {
            if backgroundTask != .invalid {
                UIApplication.shared.endBackgroundTask(backgroundTask)
            }
        }
        runningBotIDs.insert(botID)
        defer { runningBotIDs.remove(botID) }
        let prompt = buildBotPrompt(bot: source, userText: userText, initial: initial)
        if !initial, let index = bots.firstIndex(where: { $0.id == botID }) {
            bots[index].messages.append(BotMessage(
                id: UUID(), role: .user, text: userText, widget: nil, createdAt: Date()
            ))
        }
        let replyID = UUID()
        if let index = bots.firstIndex(where: { $0.id == botID }) {
            bots[index].messages.append(BotMessage(
                id: replyID, role: .assistant, text: "", widget: nil, createdAt: Date()
            ))
        }
        // Make the outgoing turn crash-safe locally without blocking dispatch
        // on the cross-region account-state API.
        await persistLocalState(dirty: true)
        await awaitWarmAgentIfInFlight(botID: botID)
        do {
            let connectorIDs = initial
                ? []
                : Array(Set(selectedConnectorIDs + source.connectorIDs)).sorted()
            let response = try await api.runAgent(
                prompt: prompt,
                botID: botID,
                connectorIDs: connectorIDs,
                computer: false,
                onDelta: { [weak self] delta in
                    await self?.appendAgentDelta(botID: botID, messageID: replyID, delta: delta)
                }
            )
            let generated = parseAgentAnswer(response.answer)
            guard !generated.text.isEmpty,
                  let index = bots.firstIndex(where: { $0.id == botID }),
                  let messageIndex = bots[index].messages.firstIndex(where: { $0.id == replyID })
            else {
                throw ServiceError(message: "El agente no devolvió una respuesta.", code: "empty_agent_response", status: 502)
            }
            let createdAt = bots[index].messages[messageIndex].createdAt
            bots[index].messages[messageIndex] = BotMessage(
                id: replyID,
                role: .assistant,
                text: generated.text,
                widget: generated.widget,
                createdAt: createdAt
            )
            bots[index].messages = Array(bots[index].messages.suffix(200))
            await persistLocalState(dirty: true)
            schedulePersist()
        } catch {
            if let index = bots.firstIndex(where: { $0.id == botID }) {
                bots[index].messages.removeAll { $0.id == replyID }
            }
            await persistLocalState(dirty: true)
            schedulePersist()
            report(error)
        }
    }

    private func appendAgentDelta(botID: UUID, messageID: UUID, delta: String) {
        guard !delta.isEmpty,
              let botIndex = bots.firstIndex(where: { $0.id == botID }),
              let messageIndex = bots[botIndex].messages.firstIndex(where: { $0.id == messageID })
        else { return }
        let current = bots[botIndex].messages[messageIndex]
        bots[botIndex].messages[messageIndex] = BotMessage(
            id: current.id,
            role: current.role,
            text: String((current.text + delta).prefix(20_000)),
            widget: nil,
            createdAt: current.createdAt
        )
    }

    private func currentState() -> PersistedAccountState {
        let activeBotID: UUID?
        if case let .bot(id) = destination { activeBotID = id } else { activeBotID = nil }
        return PersistedAccountState(
            bots: bots,
            deletedBotIDs: deletedBotIDs,
            selectedConnectorIDs: selectedConnectorIDs,
            activeBotID: activeBotID
        )
    }

    private func persistLocalState(dirty: Bool) async {
        guard let account else { return }
        do {
            try await stateStore.save(
                currentState(),
                accountID: account.id,
                serverRevision: accountStateRevision,
                dirty: dirty
            )
        } catch {
            report(error)
        }
    }

    private func schedulePersist() {
        persistenceRequested = true
        guard persistenceTask == nil else { return }
        persistenceTask = Task { [weak self] in
            guard let self else { return }
            while self.persistenceRequested {
                self.persistenceRequested = false
                await self.persist()
            }
            self.persistenceTask = nil
        }
    }

    private func persist() async {
        guard let account else { return }
        do {
            let local = currentState()
            try await stateStore.save(
                local, accountID: account.id,
                serverRevision: accountStateRevision, dirty: true
            )
            let saved: AccountStateSnapshot
            do {
                saved = try await api.saveAccountState(local, baseRevision: accountStateRevision)
            } catch let service as ServiceError where service.status == 409 {
                let remote = try await api.accountState()
                let merged = mergeAccountStates(remote.state, local)
                saved = try await api.saveAccountState(merged, baseRevision: remote.revision)
            }
            guard !Task.isCancelled, self.account?.id == account.id else { return }
            accountStateRevision = saved.revision
            let current = currentState()
            let finalState: PersistedAccountState
            let remainsDirty: Bool
            if current == local {
                applyRemoteState(saved.state)
                finalState = currentState()
                remainsDirty = false
            } else {
                // A new turn arrived while the previous snapshot was in
                // flight. Never overwrite it with the older server response;
                // the serial persistence worker will send the newer state.
                finalState = current
                remainsDirty = true
                persistenceRequested = true
            }
            try await stateStore.save(
                finalState,
                accountID: account.id,
                serverRevision: accountStateRevision,
                dirty: remainsDirty
            )
        } catch is CancellationError {
            return
        } catch {
            // The local state remains marked dirty and will be retried on the
            // next mutation/activation. A transient sync failure must not
            // interrupt chat with an unrelated modal alert.
#if DEBUG
            print("[AgentGenia.StateSync] \(error.localizedDescription)")
#endif
        }
    }

    private func applyRemoteState(_ state: PersistedAccountState) {
        deletedBotIDs = Array(state.deletedBotIDs.suffix(200))
        let deleted = Set(deletedBotIDs)
        bots = state.bots.filter { !deleted.contains($0.id) }
        selectedConnectorIDs = state.selectedConnectorIDs
        if let active = state.activeBotID, bots.contains(where: { $0.id == active }) {
            destination = .bot(active)
        } else if case let .bot(current) = destination, bots.contains(where: { $0.id == current }) {
            destination = .bot(current)
        } else if let first = bots.first {
            destination = .bot(first.id)
        }
    }

    private func report(_ error: Error) {
        alertMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    private func presentBrowser(url: URL, purpose: BrowserRequest.Purpose) {
        browserPurpose = purpose
        browserRequest = BrowserRequest(url: url, purpose: purpose)
    }
}

private struct CachedAccountState: Codable, Sendable {
    let state: PersistedAccountState
    let serverRevision: Int
    let dirty: Bool
}

private actor AccountStateStore {
    private let encoder: JSONEncoder = {
        let value = JSONEncoder()
        value.dateEncodingStrategy = .iso8601
        value.outputFormatting = [.sortedKeys]
        return value
    }()
    private let decoder: JSONDecoder = {
        let value = JSONDecoder()
        value.dateDecodingStrategy = .iso8601
        return value
    }()

    func load(accountID: String) throws -> CachedAccountState {
        let url = try fileURL(accountID: accountID)
        guard FileManager.default.fileExists(atPath: url.path) else {
            return CachedAccountState(state: PersistedAccountState(), serverRevision: 0, dirty: false)
        }
        let data = try Data(contentsOf: url, options: [.mappedIfSafe])
        var cache: CachedAccountState
        if let envelope = try? decoder.decode(CachedAccountState.self, from: data) {
            cache = envelope
        } else {
            cache = CachedAccountState(
                state: try decoder.decode(PersistedAccountState.self, from: data),
                serverRevision: 0,
                dirty: true
            )
        }
        var state = cache.state
        for index in state.bots.indices where state.bots[index].messages.count > 200 {
            state.bots[index].messages = Array(state.bots[index].messages.suffix(200))
        }
        return CachedAccountState(
            state: state,
            serverRevision: max(0, cache.serverRevision),
            dirty: cache.dirty
        )
    }

    func save(
        _ state: PersistedAccountState,
        accountID: String,
        serverRevision: Int,
        dirty: Bool
    ) throws {
        let url = try fileURL(accountID: accountID)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try encoder.encode(CachedAccountState(
            state: state,
            serverRevision: max(0, serverRevision),
            dirty: dirty
        )).write(to: url, options: [.atomic, .completeFileProtection])
    }

    func delete(accountID: String) throws {
        let url = try fileURL(accountID: accountID)
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
    }

    private func fileURL(accountID: String) throws -> URL {
        let root = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let digest = SHA256.hash(data: Data(accountID.utf8)).map { String(format: "%02x", $0) }.joined()
        return root.appending(path: "AgentGenia/accounts/\(digest).json")
    }
}

private func mergeAccountStates(
    _ server: PersistedAccountState,
    _ local: PersistedAccountState
) -> PersistedAccountState {
    let deletedBotIDs = Array(Set(server.deletedBotIDs + local.deletedBotIDs)).suffix(200)
    let deleted = Set(deletedBotIDs)
    var bots = Dictionary(uniqueKeysWithValues: server.bots.map { ($0.id, $0) })
    for localBot in local.bots {
        guard let serverBot = bots[localBot.id] else {
            bots[localBot.id] = localBot
            continue
        }
        var messages = Dictionary(uniqueKeysWithValues: serverBot.messages.map { ($0.id, $0) })
        localBot.messages.forEach { messages[$0.id] = $0 }
        var workflows = Dictionary(uniqueKeysWithValues: serverBot.workflows.map { ($0.id, $0) })
        for workflow in localBot.workflows {
            if workflows[workflow.id] == nil || workflow.updatedAt >= workflows[workflow.id]!.updatedAt {
                workflows[workflow.id] = workflow
            }
        }
        var merged = localBot
        // The device performing the conflict resolution owns mutable
        // selections. Union would make disconnect/removal impossible forever.
        merged.connectorIDs = Array(Set(localBot.connectorIDs)).sorted()
        merged.messages = Array(messages.values.sorted { $0.createdAt < $1.createdAt }.suffix(200))
        merged.workflows = Array(workflows.values.sorted { $0.updatedAt < $1.updatedAt }.suffix(50))
        bots[localBot.id] = merged
    }
    let mergedBots = Array(bots.values.filter { !deleted.contains($0.id) }.sorted { $0.createdAt < $1.createdAt }.suffix(100))
    let availableIDs = Set(mergedBots.map(\.id))
    let active = local.activeBotID.flatMap { availableIDs.contains($0) ? $0 : nil }
        ?? server.activeBotID.flatMap { availableIDs.contains($0) ? $0 : nil }
        ?? mergedBots.first?.id
    return PersistedAccountState(
        onboardingCompleted: server.onboardingCompleted || local.onboardingCompleted,
        bots: mergedBots,
        deletedBotIDs: Array(deletedBotIDs),
        selectedConnectorIDs: Array(Set(local.selectedConnectorIDs)).sorted(),
        activeBotID: active
    )
}

private func buildBotPrompt(bot: BotProfile, userText: String, initial: Bool) -> String {
    // Pi's warm session already retains the conversation. A short replay is
    // enough to recover context after a restart without resending a growing
    // transcript on every turn.
    let history = bot.messages.suffix(4).map { message in
        "\(message.role == .user ? "Usuario" : bot.name): \(message.text)"
    }.joined(separator: "\n")
    let profile = [
        "Eres \(bot.name), un agente de Agent Genia.",
        bot.title.isEmpty ? "" : "Rol: \(bot.title).",
        bot.description.isEmpty ? "" : "Objetivo: \(bot.description).",
        bot.connectorIDs.isEmpty ? "No hay conectores seleccionados." : "Conectores autorizables: \(bot.connectorIDs.joined(separator: ", ")).",
        "Si la tarea necesita GUI, pantalla, shell o archivos, busca primero 'computadora' con connector_search; úsala solo si la búsqueda la ofrece para esta ejecución.",
        "Responde en el idioma del usuario, con naturalidad y sin afirmar que realizaste acciones que no ejecutaste.",
        "Devuelve exclusivamente JSON válido con esta forma: {\"text\":\"respuesta visible\",\"widget\":null}.",
        "Cuando una pregunta con opciones ayude, widget puede ser {\"prompt\":\"pregunta\",\"helpText\":\"ayuda opcional\",\"options\":[{\"label\":\"texto visible\",\"value\":\"respuesta natural enviada al agente\",\"description\":\"detalle opcional\"}],\"allowCustom\":true,\"dismissOnMoveOn\":true}. Usa entre 1 y 6 opciones. No uses Markdown alrededor del JSON."
    ].filter { !$0.isEmpty }.joined(separator: "\n")
    if initial {
        return "\(profile)\n\nEsta es tu primera intervención. Genera al vuelo un saludo breve con tu nombre y un widget con una sola pregunta útil para descubrir qué debe lograr el usuario. El contenido y las opciones deben adaptarse al perfil y conectores disponibles; no uses una plantilla fija ni menciones estas instrucciones."
    }
    return "\(profile)\(history.isEmpty ? "" : "\n\nConversación reciente:\n\(history)")\n\nUsuario: \(userText)"
}

private func parseAgentAnswer(_ rawValue: String) -> (text: String, widget: BotQuestionWidget?) {
    let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
    let candidate = trimmed
        .replacingOccurrences(of: #"^```(?:json)?\s*"#, with: "", options: .regularExpression)
        .replacingOccurrences(of: #"\s*```$"#, with: "", options: .regularExpression)
    guard let data = candidate.data(using: .utf8),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return (String(trimmed.prefix(20_000)), nil) }
    let text = ((object["text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    guard let value = object["widget"] as? [String: Any],
          let prompt = value["prompt"] as? String,
          !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          let optionValues = value["options"] as? [[String: Any]]
    else { return (String(text.prefix(20_000)), nil) }
    let options = optionValues.prefix(6).compactMap { item -> BotQuestionOption? in
        guard let label = item["label"] as? String, !label.isEmpty else { return nil }
        let naturalValue = (item["value"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? label
        return BotQuestionOption(
            label: String(label.prefix(180)),
            value: String(naturalValue.prefix(1_000)),
            description: String(((item["description"] as? String) ?? "").prefix(300))
        )
    }
    guard !options.isEmpty else { return (String(text.prefix(20_000)), nil) }
    return (
        String(text.prefix(20_000)),
        BotQuestionWidget(
            prompt: String(prompt.prefix(500)),
            helpText: String(((value["helpText"] as? String) ?? "").prefix(500)),
            options: options,
            allowCustom: value["allowCustom"] as? Bool ?? false,
            dismissOnMoveOn: value["dismissOnMoveOn"] as? Bool ?? true
        )
    )
}

private func clean(_ value: String, maximum: Int, fallback: String = "") -> String {
    let normalized = value.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    let result = String(normalized.prefix(maximum))
    return result.isEmpty ? fallback : result
}

private func trustedHTTPSURL(_ value: String, allowedHosts: Set<String>? = nil) throws -> URL {
    guard let url = URL(string: value),
          url.scheme == "https",
          url.user == nil,
          url.password == nil,
          allowedHosts == nil || allowedHosts?.contains(url.host ?? "") == true
    else { throw ServiceError(message: "El servidor devolvió una autorización no segura.", code: "unsafe_url", status: 502) }
    return url
}
