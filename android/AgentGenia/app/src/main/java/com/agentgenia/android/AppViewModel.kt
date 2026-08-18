package com.agentgenia.android

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.agentgenia.android.data.AgentGeniaApi
import com.agentgenia.android.data.SecureStore
import com.agentgenia.android.data.ServiceException
import com.agentgenia.android.model.AccountIdentity
import com.agentgenia.android.model.AccountProfile
import com.agentgenia.android.model.BOT_COLORS
import com.agentgenia.android.model.BillingSnapshot
import com.agentgenia.android.model.BotMessage
import com.agentgenia.android.model.BotProfile
import com.agentgenia.android.model.BotShape
import com.agentgenia.android.model.BotWidgetAction
import com.agentgenia.android.model.ComputerSnapshot
import com.agentgenia.android.model.ConnectorCatalog
import com.agentgenia.android.model.ConnectorStatus
import com.agentgenia.android.model.MessageRole
import com.agentgenia.android.model.PersistedAccountState
import com.agentgenia.android.model.PendingAgentRun
import com.agentgenia.android.model.WhatsAppStatus
import kotlinx.coroutines.Job
import com.agentgenia.android.model.parseAgentAnswer
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.UUID

enum class AppPhase { Loading, SignedOut, Ready }
enum class MainSection { Agents, Plugins, Account }

data class AppUiState(
    val phase: AppPhase = AppPhase.Loading,
    val account: AccountIdentity? = null,
    val profile: AccountProfile? = null,
    val bots: List<BotProfile> = emptyList(),
    val selectedConnectorIds: List<String> = emptyList(),
    val connectorStatuses: Map<String, ConnectorStatus> = emptyMap(),
    val billing: BillingSnapshot? = null,
    val whatsApp: WhatsAppStatus? = null,
    val whatsAppLinkCode: String = "",
    val section: MainSection = MainSection.Agents,
    val selectedBotId: String? = null,
    val runningBotIds: Set<String> = emptySet(),
    val busy: Boolean = false,
    val externalUrl: String? = null,
    val computer: ComputerSnapshot? = null,
    val computerBotId: String? = null,
    val computerViewerUrl: String? = null,
    val error: String? = null,
)

class AppViewModel(
    private val api: AgentGeniaApi,
    private val store: SecureStore,
    private val appVersion: String,
) : ViewModel() {
    private val _state = MutableStateFlow(AppUiState())
    val state: StateFlow<AppUiState> = _state.asStateFlow()
    private var whatsAppPollingJob: Job? = null
    private var accountStateRevision = 0
    private var stateSyncJob: Job? = null
    private var stateSyncRequested = false
    private var onboardingCompleted = false
    private var deletedBotIds: List<String> = emptyList()
    private var pendingRuns: List<PendingAgentRun> = emptyList()
    private var pendingRecoveryJob: Job? = null

    init { bootstrap() }

    fun beginSignIn() = launchBusy {
        val started = api.beginSignIn(appVersion)
        _state.update { it.copy(externalUrl = api.validateAuthorizationUrl(started.authorizeUrl, googleOnly = true)) }
        pollSignIn(started.attemptId, started.expiresIn)
    }

    fun consumeExternalUrl() = _state.update { it.copy(externalUrl = null) }
    fun dismissComputerViewer() = _state.update { it.copy(computerViewerUrl = null) }
    fun clearError() = _state.update { it.copy(error = null) }

    fun onForeground() {
        if (_state.value.phase == AppPhase.Ready) recoverPendingRuns()
    }

    fun signOut() = viewModelScope.launch {
        whatsAppPollingJob?.cancel()
        whatsAppPollingJob = null
        stateSyncJob?.cancel()
        stateSyncJob = null
        stateSyncRequested = false
        accountStateRevision = 0
        api.signOut()
        _state.value = AppUiState(phase = AppPhase.SignedOut)
    }

    fun deleteAccount() = launchBusy {
        val accountId = _state.value.account?.id ?: return@launchBusy
        api.deleteAccount()
        val cleanupError = runCatching { store.deleteAccountState(accountId) }.exceptionOrNull()
        _state.value = AppUiState(
            phase = AppPhase.SignedOut,
            error = cleanupError?.let(::userMessage),
        )
    }

    fun selectSection(section: MainSection) {
        _state.update { it.copy(section = section) }
        when (section) {
            MainSection.Plugins -> refreshConnectors()
            MainSection.Account -> {
                refreshBilling()
                refreshWhatsApp()
            }
            else -> Unit
        }
    }

    fun createBot() {
        val current = _state.value
        if (current.account == null) return
        if (current.bots.size >= 100) {
            _state.update { it.copy(error = "Puedes tener como máximo 100 bots. Elimina uno antes de crear otro.") }
            return
        }
        val index = current.bots.size
        val shape = BotShape.entries[index % BotShape.entries.size]
        val created = BotProfile(
            color = BOT_COLORS[index % BOT_COLORS.size],
            shape = shape,
            connectorIds = current.selectedConnectorIds,
        )
        _state.update { it.copy(bots = it.bots + created, selectedBotId = created.id, section = MainSection.Agents) }
        persist()
        sendInitialMessageIfNeeded(created.id)
    }

    fun selectBot(botId: String) {
        _state.update { it.copy(selectedBotId = botId, section = MainSection.Agents) }
        persist()
        sendInitialMessageIfNeeded(botId)
    }

    fun showAgentList() {
        _state.update { it.copy(selectedBotId = null, section = MainSection.Agents) }
    }

    fun updateBot(
        botId: String,
        name: String,
        title: String,
        description: String,
        color: String,
        shape: BotShape,
        notifications: Boolean,
    ) {
        mutateBot(botId) { bot ->
            val now = System.currentTimeMillis()
            bot.copy(
                name = clean(name, 60, "Nuevo bot"),
                title = clean(title, 100),
                description = clean(description, 600),
                color = color.takeIf(BOT_COLORS::contains) ?: bot.color,
                shape = shape,
                notificationsEnabled = notifications,
                updatedAt = now,
                profileRevision = now,
                notificationRevision = now,
            )
        }
    }

    fun sendMessage(botId: String, value: String, action: BotWidgetAction? = null) {
        val message = clean(value, 20_000)
        if (message.isEmpty()) return
        runAgent(botId, message, initial = false, action = action)
    }

    fun refreshConnectors() = viewModelScope.launch {
        runCatching { api.connectors() }
            .onSuccess(::applyConnectorSnapshot)
            .onFailure(::report)
    }

    fun connect(connectorId: String) = launchBusy {
        val started = api.startConnector(connectorId)
        _state.update { it.copy(externalUrl = api.validateAuthorizationUrl(started.authorizeUrl)) }
        pollConnector(started.attemptId, connectorId)
    }

    fun disconnect(connectorId: String) = launchBusy {
        api.disconnectConnector(connectorId)
        _state.update { current ->
            current.copy(
                selectedConnectorIds = current.selectedConnectorIds - connectorId,
                bots = current.bots.map {
                    if (connectorId in it.connectorIds) it.copy(
                        connectorIds = it.connectorIds - connectorId,
                        updatedAt = System.currentTimeMillis(),
                        connectorAssignmentRevision = System.currentTimeMillis(),
                    ) else it
                },
            )
        }
        persist()
        refreshConnectors()
    }

    fun refreshBilling() = viewModelScope.launch {
        runCatching { api.billing() }
            .onSuccess { billing -> _state.update { it.copy(billing = billing) } }
            .onFailure(::report)
    }

    fun openCheckout(tier: String) = launchBusy {
        _state.update { it.copy(externalUrl = api.checkoutUrl(tier)) }
    }

    fun openBillingPortal() = launchBusy {
        _state.update { it.copy(externalUrl = api.portalUrl()) }
    }

    fun refreshWhatsApp() = viewModelScope.launch {
        runCatching { api.whatsAppStatus() }
            .onSuccess { status -> _state.update { it.copy(whatsApp = status) } }
            .onFailure(::report)
    }

    fun startWhatsAppLink() = launchBusy {
        val started = api.startWhatsAppLink()
        _state.update { it.copy(externalUrl = started.url, whatsAppLinkCode = started.code) }
        whatsAppPollingJob?.cancel()
        whatsAppPollingJob = viewModelScope.launch {
            while (System.currentTimeMillis() / 1_000 < started.expiresAt) {
                delay(2_000)
                val status = runCatching { api.whatsAppStatus() }.getOrNull() ?: continue
                _state.update { it.copy(whatsApp = status) }
                if (status.connected) {
                    _state.update { it.copy(whatsAppLinkCode = "") }
                    return@launch
                }
            }
            _state.update { it.copy(whatsAppLinkCode = "") }
        }
    }

    fun unlinkWhatsApp() = launchBusy {
        whatsAppPollingJob?.cancel()
        whatsAppPollingJob = null
        val status = api.unlinkWhatsApp()
        _state.update { it.copy(whatsApp = status, whatsAppLinkCode = "") }
    }

    fun loadComputer(botId: String) = viewModelScope.launch {
        _state.update { it.copy(computerBotId = botId) }
        runCatching { api.computerStatus(botId) }
            .onSuccess { snapshot -> _state.update { it.copy(computer = snapshot, computerBotId = botId) } }
            .onFailure(::report)
    }

    fun ensureComputer(botId: String) = launchBusy {
        val bot = _state.value.bots.firstOrNull { it.id == botId } ?: return@launchBusy
        val snapshot = api.ensureComputer(botId, bot.name)
        _state.update {
            it.copy(
                computer = snapshot,
                computerBotId = botId,
                computerViewerUrl = snapshot.viewerUrl.takeIf { url -> snapshot.state.name == "Running" && url.startsWith("https://") },
            )
        }
    }

    fun openComputer(botId: String) {
        val current = _state.value
        val snapshot = current.computer.takeIf { current.computerBotId == botId }
        val url = snapshot?.viewerUrl?.takeIf { snapshot.state.name == "Running" && it.startsWith("https://") }
        if (url != null) _state.update { it.copy(computerViewerUrl = url) }
    }

    fun handBackComputer(botId: String) = launchBusy {
        val snapshot = api.handBackComputer(botId)
        _state.update { it.copy(computer = snapshot, computerBotId = botId, computerViewerUrl = null) }
    }

    fun deleteComputer(botId: String) = launchBusy {
        api.deleteComputer(botId)
        val snapshot = api.computerStatus(botId)
        _state.update {
            it.copy(computer = snapshot, computerBotId = botId, computerViewerUrl = null)
        }
    }

    private fun bootstrap() = viewModelScope.launch {
        val session = runCatching { api.restoreSession() }.getOrNull()
        if (session == null) {
            _state.update { it.copy(phase = AppPhase.SignedOut) }
            return@launch
        }
        runCatching { activate(session.account) }
            .onFailure {
                api.signOut()
                _state.value = AppUiState(phase = AppPhase.SignedOut, error = userMessage(it))
            }
    }

    private suspend fun activate(account: AccountIdentity) {
        val profile = api.me()
        val persisted = store.readAccountState(account.id)
        onboardingCompleted = persisted.onboardingCompleted
        deletedBotIds = persisted.deletedBotIds
        pendingRuns = persisted.pendingRuns
        _state.value = AppUiState(
            phase = AppPhase.Ready,
            account = account,
            profile = profile,
            bots = persisted.bots,
            selectedConnectorIds = persisted.selectedConnectorIds,
            selectedBotId = persisted.activeBotId?.takeIf { id -> persisted.bots.any { it.id == id } }
                ?: persisted.bots.firstOrNull()?.id,
            section = MainSection.Agents,
        )
        val accountState = viewModelScope.async { runCatching { api.accountState() } }
        val connectors = viewModelScope.async { runCatching { api.connectors() } }
        val billing = viewModelScope.async { runCatching { api.billing() }.getOrNull() }
        val whatsApp = viewModelScope.async { runCatching { api.whatsAppStatus() }.getOrNull() }
        val accountStateResult = accountState.await()
        accountStateResult.onSuccess { remote ->
            accountStateRevision = remote.revision
            val local = currentAccountState()
            val resolved = mergeAccountStates(remote.state, local)
            applyAccountState(resolved)
            writeLocalState(account.id, resolved)
            if (resolved != remote.state) scheduleStateSync()
        }.onFailure {
            // Keep the encrypted local cache usable while a sleeping or
            // temporarily unavailable backend wakes up. The serialized sync
            // worker retries on this and the next local mutation.
            scheduleStateSync()
        }
        val connectorResult = connectors.await()
        val billingValue = billing.await()
        val whatsAppValue = whatsApp.await()
        connectorResult
            .onSuccess(::applyConnectorSnapshot)
            .onFailure { error ->
                _state.update {
                    it.copy(
                        billing = billingValue,
                        whatsApp = whatsAppValue,
                        error = "No pudimos actualizar los conectores; conservamos tu configuración local. ${userMessage(error)}",
                    )
                }
            }
        if (connectorResult.isSuccess) _state.update { it.copy(billing = billingValue, whatsApp = whatsAppValue) }
        recoverPendingRuns()
    }

    private fun applyConnectorSnapshot(statuses: List<ConnectorStatus>) {
        val knownIds = ConnectorCatalog.all.mapTo(mutableSetOf()) { it.id }
        val connectedIds = statuses.asSequence()
            .filter { it.connected && it.connectorId in knownIds }
            .map(ConnectorStatus::connectorId)
            .distinct()
            .sorted()
            .toList()
        _state.update { current ->
            current.copy(
                selectedConnectorIds = connectedIds,
                bots = current.bots.map { bot ->
                    val reconciled = bot.connectorIds.filter(connectedIds.toSet()::contains)
                    if (reconciled == bot.connectorIds) bot else bot.copy(
                        connectorIds = reconciled,
                        updatedAt = System.currentTimeMillis(),
                        connectorAssignmentRevision = System.currentTimeMillis(),
                    )
                },
                connectorStatuses = statuses.associateBy(ConnectorStatus::connectorId),
            )
        }
        persist()
    }

    private suspend fun pollSignIn(attemptId: String, expiresIn: Int = 600) {
        val attempts = ((expiresIn.coerceIn(30, 1_800) + 1) / 2).coerceAtLeast(1)
        repeat(attempts) {
            val status = api.authStatus(attemptId)
            when (status.status) {
                "complete" -> {
                    val session = api.completeSignIn(status)
                    _state.update { it.copy(externalUrl = null) }
                    activate(session.account)
                    return
                }
                "error" -> throw ServiceException(status.message ?: "Google rechazó el inicio de sesión.", "auth_error", 400)
            }
            delay(2_000)
        }
        throw ServiceException("El inicio de sesión expiró.", "auth_timeout", 408)
    }

    private suspend fun pollConnector(attemptId: String, connectorId: String) {
        repeat(300) {
            val status = api.connectorStatus(attemptId)
            when (status.status) {
                "complete" -> {
                    _state.update { current ->
                        current.copy(
                            externalUrl = null,
                            selectedConnectorIds = (current.selectedConnectorIds + connectorId).distinct(),
                            bots = current.bots.map { bot ->
                                if (bot.id == current.selectedBotId) bot.copy(
                                    connectorIds = (bot.connectorIds + connectorId).distinct(),
                                    updatedAt = System.currentTimeMillis(),
                                    connectorAssignmentRevision = System.currentTimeMillis(),
                                ) else bot
                            },
                        )
                    }
                    persist()
                    refreshConnectors()
                    return
                }
                "error" -> throw ServiceException(status.message ?: "El proveedor rechazó la conexión.", "connector_error", 400)
            }
            delay(2_000)
        }
        throw ServiceException("La conexión expiró.", "connector_timeout", 408)
    }

    private fun sendInitialMessageIfNeeded(botId: String) {
        val bot = _state.value.bots.firstOrNull { it.id == botId } ?: return
        if (shouldSendInitialBotMessage(_state.value.profile?.tier, bot)) {
            runAgent(botId, "", initial = true)
        }
    }

    private fun runAgent(botId: String, userText: String, initial: Boolean, action: BotWidgetAction? = null) = viewModelScope.launch {
        val original = _state.value.bots.firstOrNull { it.id == botId } ?: return@launch
        if (botId in _state.value.runningBotIds) return@launch
        _state.update { it.copy(runningBotIds = it.runningBotIds + botId) }
        val turnId = if (initial) "initial-$botId" else UUID.randomUUID().toString()
        if (!initial) mutateBot(botId, persistAfter = false) { bot ->
            val now = System.currentTimeMillis()
            bot.copy(
                messages = (bot.messages + BotMessage(id = turnId, role = MessageRole.User, text = userText)).takeLast(200),
                updatedAt = now,
                conversationRevision = now,
            )
        }
        if (!initial) pendingRuns = (pendingRuns.filterNot { it.idempotencyKey == turnId } + PendingAgentRun(
            turnId = turnId, idempotencyKey = turnId, botId = botId,
        )).takeLast(100)
        persist()
        try {
            val current = _state.value
            val connectors = original.connectorIds.distinct().sorted()
            val prompt = buildBotPrompt(original.copy(connectorIds = connectors), userText, initial)
            val generated = parseAgentAnswer(api.runAgent(
                prompt = prompt,
                botId = botId,
                connectorIds = connectors,
                idempotencyKey = turnId,
                executionMode = if (initial) "chat" else "auto",
                chatPrompt = if (initial) prompt else buildDirectChatPrompt(original, userText),
                userMessage = userText,
                approval = action,
            ))
            if (generated.text.isBlank() && generated.widget == null) {
                throw ServiceException("El agente no devolvió una respuesta.", "empty_agent_response", 502)
            }
            mutateBot(botId, persistAfter = false) { bot ->
                val now = System.currentTimeMillis()
                bot.copy(messages = (bot.messages + BotMessage(
                    id = if (initial) botId else UUID.randomUUID().toString(),
                    role = MessageRole.Assistant, text = generated.text, widget = generated.widget,
                )).takeLast(200), updatedAt = now, conversationRevision = now)
            }
            persist()
            pendingRuns = pendingRuns.filterNot { it.idempotencyKey == turnId }
            persist()
        } catch (error: Throwable) {
            report(error)
        } finally {
            _state.update { it.copy(runningBotIds = it.runningBotIds - botId) }
        }
    }

    private fun mutateBot(botId: String, persistAfter: Boolean = true, block: (BotProfile) -> BotProfile) {
        _state.update { current ->
            current.copy(bots = current.bots.map { if (it.id == botId) block(it) else it })
        }
        if (persistAfter) persist()
    }

    private fun persist() {
        val current = _state.value
        val accountId = current.account?.id ?: return
        val snapshot = currentAccountState()
        runCatching { writeLocalState(accountId, snapshot) }.onFailure(::report)
        scheduleStateSync()
    }

    private fun currentAccountState() = PersistedAccountState(
        onboardingCompleted = onboardingCompleted,
        bots = _state.value.bots,
        selectedConnectorIds = _state.value.selectedConnectorIds,
        activeBotId = _state.value.selectedBotId,
        deletedBotIds = deletedBotIds,
        pendingRuns = pendingRuns,
    )

    private fun recoverPendingRuns() {
        if (pendingRecoveryJob?.isActive == true) return
        pendingRecoveryJob = viewModelScope.launch {
            pendingRuns.toList().forEach { pending ->
                if (_state.value.bots.none { it.id == pending.botId }) return@forEach
                pendingRuns = pendingRuns.map { if (it.idempotencyKey == pending.idempotencyKey)
                    it.copy(status = "recovering", lastRecoveryAt = System.currentTimeMillis()) else it }
                persist()
                _state.update { it.copy(runningBotIds = it.runningBotIds + pending.botId) }
                try {
                    val answer = api.recoverAgentAnswer(pending.idempotencyKey) ?: return@forEach
                    val generated = parseAgentAnswer(answer)
                    if (generated.text.isBlank() && generated.widget == null) return@forEach
                    mutateBot(pending.botId, persistAfter = false) { bot ->
                        val now = System.currentTimeMillis()
                        bot.copy(
                            messages = (bot.messages + BotMessage(
                                role = MessageRole.Assistant, text = generated.text, widget = generated.widget,
                            )).takeLast(200),
                            updatedAt = now, conversationRevision = now,
                        )
                    }
                    pendingRuns = pendingRuns.filterNot { it.idempotencyKey == pending.idempotencyKey }
                    persist()
                } catch (error: Throwable) {
                    report(error)
                } finally {
                    _state.update { it.copy(runningBotIds = it.runningBotIds - pending.botId) }
                }
            }
        }
    }

    private fun writeLocalState(accountId: String, snapshot: PersistedAccountState) {
        store.writeAccountState(accountId, snapshot)
    }

    private fun applyAccountState(snapshot: PersistedAccountState) {
        onboardingCompleted = snapshot.onboardingCompleted
        deletedBotIds = snapshot.deletedBotIds.takeLast(1_000)
        pendingRuns = snapshot.pendingRuns.takeLast(100)
        val deleted = deletedBotIds.toSet()
        val bots = snapshot.bots.filterNot { it.id in deleted }
        _state.update { current ->
            val active = snapshot.activeBotId?.takeIf { id -> bots.any { it.id == id } }
                ?: current.selectedBotId?.takeIf { id -> bots.any { it.id == id } }
                ?: bots.firstOrNull()?.id
            current.copy(
                bots = bots,
                selectedConnectorIds = snapshot.selectedConnectorIds,
                selectedBotId = active,
            )
        }
    }

    private fun scheduleStateSync() {
        if (_state.value.account == null) return
        stateSyncRequested = true
        if (stateSyncJob != null) return
        stateSyncJob = viewModelScope.launch {
            var retryDelay = 250L
            while (stateSyncRequested) {
                stateSyncRequested = false
                val account = _state.value.account ?: break
                val local = currentAccountState()
                try {
                    val saved = try {
                        api.saveAccountState(local, accountStateRevision)
                    } catch (error: ServiceException) {
                        if (error.status != 409) throw error
                        val remote = api.accountState()
                        val merged = mergeAccountStates(remote.state, currentAccountState())
                        api.saveAccountState(merged, remote.revision)
                    }
                    if (_state.value.account?.id != account.id) break
                    accountStateRevision = saved.revision
                    if (currentAccountState() == local) {
                        applyAccountState(saved.state)
                        writeLocalState(account.id, currentAccountState())
                    } else {
                        stateSyncRequested = true
                    }
                    retryDelay = 250L
                } catch (_: Throwable) {
                    // Local data stays encrypted and marked for retry; a
                    // transient sync failure must not interrupt an agent turn.
                    stateSyncRequested = true
                    delay(retryDelay)
                    retryDelay = (retryDelay * 2).coerceAtMost(30_000L)
                }
            }
            stateSyncJob = null
            if (stateSyncRequested) scheduleStateSync()
        }
    }

    private fun launchBusy(block: suspend () -> Unit) = viewModelScope.launch {
        if (_state.value.busy) return@launch
        _state.update { it.copy(busy = true) }
        try { block() } catch (error: Throwable) { report(error) }
        finally { _state.update { it.copy(busy = false) } }
    }

    private fun report(error: Throwable) = _state.update { it.copy(error = userMessage(error)) }
    private fun userMessage(error: Throwable) = (error as? ServiceException)?.message ?: error.message ?: "Ocurrió un error inesperado."

    companion object {
        fun factory(application: AgentGeniaApplication): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T = AppViewModel(
                    application.api,
                    application.secureStore,
                    BuildConfig.VERSION_NAME,
                ) as T
            }
    }
}

/** The backend, not a cached client tier label, owns model entitlements. */
@Suppress("UNUSED_PARAMETER")
internal fun shouldSendInitialBotMessage(tier: String?, bot: BotProfile?): Boolean =
    bot?.messages?.isEmpty() == true

internal fun buildBotPrompt(bot: BotProfile, userText: String, initial: Boolean): String {
    val history = bot.messages.takeLast(4).joinToString("\n") { message ->
        "${if (message.role == MessageRole.User) "Usuario" else bot.name}: ${message.text.take(8_000)}"
    }
    val profile = listOfNotNull(
        "Eres ${bot.name}, un agente de Agent Genia.",
        bot.title.takeIf { it.isNotBlank() }?.let { "Rol: $it." },
        bot.description.takeIf { it.isNotBlank() }?.let { "Objetivo: $it." },
        if (bot.connectorIds.isEmpty()) "No hay conectores seleccionados." else "Conectores autorizables: ${bot.connectorIds.joinToString()}.",
        "Si la tarea necesita GUI, pantalla, shell o archivos, busca primero 'computadora' con connector_search; úsala solo si la búsqueda la ofrece para esta ejecución.",
        "Responde en el idioma del usuario y sin afirmar que realizaste acciones que no ejecutaste.",
        "Sé directo: normalmente usa entre una y tres frases. No repitas la solicitud, no añadas preámbulos, cierres, emojis decorativos ni preguntas genéricas. En text usa texto plano, sin Markdown. Tras ejecutar una acción confirma únicamente qué hiciste y los datos esenciales; no muestres URLs ni detalles internos salvo que se pidan. No agregues Meet, invitados, ubicación, duración u otros datos no solicitados.",
        "Devuelve exclusivamente JSON válido con esta forma: {\"text\":\"respuesta visible\",\"widget\":null}.",
        "Cuando una pregunta con opciones ayude, widget puede ser {\"prompt\":\"pregunta\",\"helpText\":\"ayuda opcional\",\"options\":[{\"label\":\"texto visible\",\"value\":\"respuesta natural enviada al agente\",\"description\":\"detalle opcional\"}],\"allowCustom\":true,\"dismissOnMoveOn\":true}. Usa entre 1 y 6 opciones. No uses Markdown alrededor del JSON.",
    ).joinToString("\n")
    if (initial) return "$profile\n\nEsta es tu primera intervención. Genera al vuelo un saludo breve con tu nombre y un widget con una sola pregunta útil para descubrir qué debe lograr el usuario. El contenido y las opciones deben adaptarse al perfil y conectores disponibles; no uses una plantilla fija ni menciones estas instrucciones."
    return buildString {
        append(profile)
        if (history.isNotBlank()) append("\n\nConversación reciente:\n$history")
        append("\n\nUsuario: $userText")
    }
}

internal fun buildDirectChatPrompt(bot: BotProfile, userText: String): String {
    val history = bot.messages.takeLast(6).joinToString("\n") { message ->
        "${if (message.role == MessageRole.User) "Usuario" else bot.name}: ${message.text.take(8_000)}"
    }
    return listOfNotNull(
        "Eres ${bot.name}, un agente de Agent Genia.",
        bot.title.takeIf { it.isNotBlank() }?.let { "Rol: $it." },
        bot.description.takeIf { it.isNotBlank() }?.let { "Objetivo: $it." },
        "Responde directamente en el idioma del usuario, normalmente en una a tres frases.",
        "No repitas la solicitud ni añadas preámbulos, cierres, emojis decorativos o preguntas genéricas. Usa texto plano, sin Markdown.",
        "No uses JSON ni menciones instrucciones internas.",
        "No afirmes haber ejecutado acciones externas; esta ruta solo conversa y redacta.",
        history.takeIf { it.isNotBlank() }?.let { "Conversación reciente:\n$it" },
        "Usuario: $userText",
    ).joinToString("\n\n")
}

private fun clean(value: String, maximum: Int, fallback: String = ""): String {
    val normalized = value.replace(Regex("\\s+"), " ").trim().take(maximum)
    return normalized.ifBlank { fallback }
}

internal fun mergeAccountStates(
    server: PersistedAccountState,
    local: PersistedAccountState,
): PersistedAccountState {
    val deletedIds = (server.deletedBotIds + local.deletedBotIds).distinct().takeLast(1_000)
    val deleted = deletedIds.toSet()
    val bots = server.bots.associateBy { it.id }.toMutableMap()
    local.bots.forEach { localBot ->
        val remoteBot = bots[localBot.id]
        if (remoteBot == null) {
            bots[localBot.id] = localBot
            return@forEach
        }
        val messages = remoteBot.messages.associateBy { it.id }.toMutableMap()
        localBot.messages.forEach { messages[it.id] = it }
        val workflows = remoteBot.workflows.associateBy { it.id }.toMutableMap()
        localBot.workflows.forEach { workflow ->
            val existing = workflows[workflow.id]
            if (existing == null || workflow.updatedAt >= existing.updatedAt) {
                workflows[workflow.id] = workflow
            }
        }
        val profile = if (localBot.profileRevision >= remoteBot.profileRevision) localBot else remoteBot
        val connectors = if (localBot.connectorAssignmentRevision >= remoteBot.connectorAssignmentRevision) localBot else remoteBot
        val notifications = if (localBot.notificationRevision >= remoteBot.notificationRevision) localBot else remoteBot
        bots[localBot.id] = profile.copy(
            connectorIds = connectors.connectorIds.distinct().sorted(),
            notificationsEnabled = notifications.notificationsEnabled,
            messages = messages.values.sortedBy { it.createdAt }.takeLast(200),
            workflows = workflows.values.sortedBy { it.updatedAt }.takeLast(50),
            updatedAt = maxOf(localBot.updatedAt, remoteBot.updatedAt),
            profileRevision = maxOf(localBot.profileRevision, remoteBot.profileRevision),
            connectorAssignmentRevision = maxOf(localBot.connectorAssignmentRevision, remoteBot.connectorAssignmentRevision),
            notificationRevision = maxOf(localBot.notificationRevision, remoteBot.notificationRevision),
            conversationRevision = maxOf(localBot.conversationRevision, remoteBot.conversationRevision),
            workflowRevision = maxOf(localBot.workflowRevision, remoteBot.workflowRevision),
        )
    }
    val mergedBots = bots.values
        .filterNot { it.id in deleted }
        .sortedBy { it.createdAt }
        .takeLast(100)
    val available = mergedBots.mapTo(mutableSetOf()) { it.id }
    val active = local.activeBotId?.takeIf(available::contains)
        ?: server.activeBotId?.takeIf(available::contains)
        ?: mergedBots.firstOrNull()?.id
    val pendingByKey = server.pendingRuns.associateBy { it.idempotencyKey }.toMutableMap()
    local.pendingRuns.forEach { pendingByKey[it.idempotencyKey] = it }
    val pending = pendingByKey.values.filter { run ->
        val bot = mergedBots.firstOrNull { it.id == run.botId } ?: return@filter false
        val turnIndex = bot.messages.indexOfFirst { it.id == run.turnId }
        turnIndex >= 0 && bot.messages.drop(turnIndex + 1).none { it.role == MessageRole.Assistant }
    }.take(100)
    return PersistedAccountState(
        onboardingCompleted = server.onboardingCompleted || local.onboardingCompleted,
        bots = mergedBots,
        // OAuth connection state is authoritative on the server. A stale
        // local cache must not resurrect a connector after revocation.
        selectedConnectorIds = server.selectedConnectorIds.distinct().sorted(),
        activeBotId = active,
        deletedBotIds = deletedIds,
        pendingRuns = pending,
    )
}
