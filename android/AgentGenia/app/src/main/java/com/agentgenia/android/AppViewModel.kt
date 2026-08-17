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
import com.agentgenia.android.model.ComputerSnapshot
import com.agentgenia.android.model.ConnectorCatalog
import com.agentgenia.android.model.ConnectorStatus
import com.agentgenia.android.model.MessageRole
import com.agentgenia.android.model.PersistedAccountState
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

    init { bootstrap() }

    fun beginSignIn() = launchBusy {
        val started = api.beginSignIn(appVersion)
        _state.update { it.copy(externalUrl = api.validateAuthorizationUrl(started.authorizeUrl, googleOnly = true)) }
        pollSignIn(started.attemptId)
    }

    fun consumeExternalUrl() = _state.update { it.copy(externalUrl = null) }
    fun dismissComputerViewer() = _state.update { it.copy(computerViewerUrl = null) }
    fun clearError() = _state.update { it.copy(error = null) }

    fun signOut() = viewModelScope.launch {
        whatsAppPollingJob?.cancel()
        whatsAppPollingJob = null
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
            bot.copy(
                name = clean(name, 60, "Nuevo bot"),
                title = clean(title, 120),
                description = clean(description, 600),
                color = color.takeIf(BOT_COLORS::contains) ?: bot.color,
                shape = shape,
                notificationsEnabled = notifications,
            )
        }
    }

    fun sendMessage(botId: String, value: String) {
        val message = clean(value, 20_000)
        if (message.isEmpty()) return
        runAgent(botId, message, initial = false)
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
                bots = current.bots.map { it.copy(connectorIds = it.connectorIds - connectorId) },
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
        val connectors = viewModelScope.async { runCatching { api.connectors() } }
        val billing = viewModelScope.async { runCatching { api.billing() }.getOrNull() }
        val whatsApp = viewModelScope.async { runCatching { api.whatsAppStatus() }.getOrNull() }
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
    }

    private fun applyConnectorSnapshot(statuses: List<ConnectorStatus>) {
        val knownIds = ConnectorCatalog.all.mapTo(mutableSetOf()) { it.id }
        val connectedIds = statuses.asSequence()
            .filter { it.connected && it.connectorId in knownIds }
            .map(ConnectorStatus::connectorId)
            .distinct()
            .sorted()
            .toList()
        val connectedSet = connectedIds.toSet()
        _state.update { current ->
            current.copy(
                selectedConnectorIds = connectedIds,
                connectorStatuses = statuses.associateBy(ConnectorStatus::connectorId),
                bots = current.bots.map { bot ->
                    bot.copy(connectorIds = bot.connectorIds.filter(connectedSet::contains))
                },
            )
        }
        persist()
    }

    private suspend fun pollSignIn(attemptId: String) {
        repeat(120) {
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
        if (_state.value.profile?.tier == "free") return
        val bot = _state.value.bots.firstOrNull { it.id == botId } ?: return
        if (bot.messages.isEmpty()) runAgent(botId, "", initial = true)
    }

    private fun runAgent(botId: String, userText: String, initial: Boolean) = viewModelScope.launch {
        val original = _state.value.bots.firstOrNull { it.id == botId } ?: return@launch
        if (botId in _state.value.runningBotIds) return@launch
        _state.update { it.copy(runningBotIds = it.runningBotIds + botId) }
        val turnId = if (initial) "initial-$botId" else UUID.randomUUID().toString()
        if (!initial) mutateBot(botId, persistAfter = false) { bot ->
            bot.copy(messages = (bot.messages + BotMessage(id = turnId, role = MessageRole.User, text = userText)).takeLast(200))
        }
        persist()
        try {
            val current = _state.value
            val connectors = (current.selectedConnectorIds + original.connectorIds).distinct().sorted()
            val prompt = buildBotPrompt(original.copy(connectorIds = connectors), userText, initial)
            val generated = parseAgentAnswer(api.runAgent(
                prompt = prompt,
                botId = botId,
                connectorIds = connectors,
                idempotencyKey = turnId,
                executionMode = if (initial) "chat" else "auto",
                chatPrompt = if (initial) prompt else buildDirectChatPrompt(original, userText),
                userMessage = userText,
            ))
            if (generated.text.isBlank()) throw ServiceException("El agente no devolvió una respuesta.", "empty_agent_response", 502)
            mutateBot(botId, persistAfter = false) { bot ->
                bot.copy(messages = (bot.messages + BotMessage(
                    role = MessageRole.Assistant, text = generated.text, widget = generated.widget,
                )).takeLast(200))
            }
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
        runCatching {
            store.writeAccountState(
                accountId,
                PersistedAccountState(current.bots, current.selectedConnectorIds, current.selectedBotId),
            )
        }.onFailure(::report)
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

internal fun buildBotPrompt(bot: BotProfile, userText: String, initial: Boolean): String {
    val history = bot.messages.takeLast(20).joinToString("\n") { message ->
        "${if (message.role == MessageRole.User) "Usuario" else bot.name}: ${message.text}"
    }
    val profile = listOfNotNull(
        "Eres ${bot.name}, un agente de Agent Genia.",
        bot.title.takeIf { it.isNotBlank() }?.let { "Rol: $it." },
        bot.description.takeIf { it.isNotBlank() }?.let { "Objetivo: $it." },
        if (bot.connectorIds.isEmpty()) "No hay conectores seleccionados." else "Conectores autorizables: ${bot.connectorIds.joinToString()}.",
        "Si la tarea necesita GUI, pantalla, shell o archivos, busca primero 'computadora' con connector_search; úsala solo si la búsqueda la ofrece para esta ejecución.",
        "Responde en el idioma del usuario, con naturalidad y sin afirmar que realizaste acciones que no ejecutaste.",
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
        "${if (message.role == MessageRole.User) "Usuario" else bot.name}: ${message.text}"
    }
    return listOfNotNull(
        "Eres ${bot.name}, un agente de Agent Genia.",
        bot.title.takeIf { it.isNotBlank() }?.let { "Rol: $it." },
        bot.description.takeIf { it.isNotBlank() }?.let { "Objetivo: $it." },
        "Responde directamente en el idioma del usuario, con naturalidad y concisión.",
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
