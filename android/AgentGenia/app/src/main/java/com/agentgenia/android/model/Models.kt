package com.agentgenia.android.model

import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

data class AccountIdentity(
    val id: String,
    val email: String,
    val name: String,
    val picture: String,
)

data class AccountSession(
    val token: String,
    val refreshToken: String,
    val expiresAt: Long,
    val account: AccountIdentity,
)

data class AccountProfile(
    val userId: String,
    val name: String,
    val email: String,
    val tier: String,
    val tierLabel: String,
)

enum class BotShape { Circle, Bean, Square, Capsule, Triangle, Hexagon, Cloud, Drop }

data class BotQuestionOption(
    val label: String,
    val value: String,
    val description: String = "",
)

data class BotQuestionWidget(
    val prompt: String,
    val helpText: String = "",
    val options: List<BotQuestionOption>,
    val allowCustom: Boolean = false,
    val dismissOnMoveOn: Boolean = true,
)

enum class MessageRole { User, Assistant }

data class BotMessage(
    val id: String = UUID.randomUUID().toString(),
    val role: MessageRole,
    val text: String,
    val widget: BotQuestionWidget? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

data class BotProfile(
    val id: String = UUID.randomUUID().toString().lowercase(),
    val name: String = "Nuevo bot",
    val title: String = "",
    val description: String = "",
    val color: String = BOT_COLORS.first(),
    val shape: BotShape = BotShape.Bean,
    val avatarDataUrl: String = "",
    val notificationsEnabled: Boolean = true,
    val connectorIds: List<String> = emptyList(),
    val messages: List<BotMessage> = emptyList(),
    val workflows: List<BotWorkflow> = emptyList(),
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = createdAt,
)

data class BotWorkflow(
    val id: String,
    val title: String,
    val summary: String = "",
    val steps: List<String> = emptyList(),
    val recordingId: String = "",
    val recordingMimeType: String = "",
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
    val lastRunAt: Long? = null,
)

data class PersistedAccountState(
    val onboardingCompleted: Boolean = false,
    val bots: List<BotProfile> = emptyList(),
    val selectedConnectorIds: List<String> = emptyList(),
    val activeBotId: String? = null,
    val deletedBotIds: List<String> = emptyList(),
)

data class ConnectorDefinition(
    val id: String,
    val name: String,
    val category: String,
    val summary: String,
)

data class ConnectorStatus(
    val connectorId: String,
    val provider: String?,
    val available: Boolean,
    val connected: Boolean,
    val account: String,
    val reason: String,
)

data class BillingPlan(
    val name: String,
    val amount: Int,
    val currency: String,
    val interval: String,
    val fiveHourCredits: Int,
    val sevenDayCredits: Int,
    val monthlyCredits: Int,
    val maxConcurrentRuns: Int,
)

data class BillingSubscription(
    val status: String,
    val tier: String,
    val cancelAtPeriodEnd: Boolean,
    val currentPeriodEnd: Long?,
)

data class BillingSnapshot(
    val configured: Boolean,
    val tier: String,
    val customer: Boolean,
    val subscription: BillingSubscription?,
    val plans: Map<String, BillingPlan>,
)

data class WhatsAppStatus(
    val configured: Boolean,
    val connected: Boolean,
    val displayName: String,
    val phoneHint: String,
    val activeBotId: String?,
)

data class WhatsAppLinkStart(
    val code: String,
    val expiresAt: Long,
    val url: String,
)

enum class ComputerState { Disabled, Pulling, Running, Hibernated, Off, Error }

data class ComputerSnapshot(
    val configured: Boolean,
    val botId: String,
    val provider: String?,
    val state: ComputerState,
    val viewerUrl: String,
    val viewerExpiresAt: Long,
    val reason: String,
)

data class GeneratedAnswer(val text: String, val widget: BotQuestionWidget?)

val BOT_COLORS = listOf(
    "#A66D35", "#FF2F43", "#FF6A00", "#FF9300", "#08BE70",
    "#11B9A9", "#2F91F5", "#8654ED", "#F35CA7", "#808080",
)

fun AccountIdentity.toJson() = JSONObject()
    .put("id", id).put("email", email).put("name", name).put("picture", picture)

fun JSONObject.toAccountIdentity() = AccountIdentity(
    id = optString("id"),
    email = optString("email"),
    name = optString("name"),
    picture = optString("picture"),
)

fun AccountSession.toJson() = JSONObject()
    .put("token", token)
    .put("refresh_token", refreshToken)
    .put("expires_at", expiresAt)
    .put("account", account.toJson())

fun JSONObject.toAccountSession() = AccountSession(
    token = getString("token"),
    refreshToken = getString("refresh_token"),
    expiresAt = getLong("expires_at"),
    account = getJSONObject("account").toAccountIdentity(),
)

fun PersistedAccountState.toJson() = JSONObject()
    .put("version", 2)
    .put("onboardingCompleted", onboardingCompleted)
    .put("bots", JSONArray().also { array -> bots.forEach { array.put(it.toJson()) } })
    .put("deletedBotIds", JSONArray(deletedBotIds))
    .put("selectedConnectorIds", JSONArray(selectedConnectorIds))
    .put("activeBotId", activeBotId ?: JSONObject.NULL)

fun JSONObject.toPersistedAccountState(): PersistedAccountState {
    val botArray = optJSONArray("bots") ?: JSONArray()
    val selected = optJSONArray("selectedConnectorIds") ?: JSONArray()
    val deleted = optJSONArray("deletedBotIds") ?: JSONArray()
    return PersistedAccountState(
        onboardingCompleted = optBoolean("onboardingCompleted"),
        bots = List(botArray.length()) { botArray.getJSONObject(it).toBotProfile() },
        selectedConnectorIds = List(selected.length()) { selected.getString(it) },
        activeBotId = optNullableString("activeBotId"),
        deletedBotIds = List(deleted.length()) { deleted.getString(it) }.takeLast(1_000),
    )
}

fun BotProfile.toJson() = JSONObject()
    .put("id", id)
    .put("name", name)
    .put("title", title)
    .put("description", description)
    .put("color", color)
    .put("shape", shape.name.lowercase())
    .put("avatarDataUrl", avatarDataUrl)
    .put("notificationsEnabled", notificationsEnabled)
    .put("connectorIds", JSONArray(connectorIds))
    .put("messages", JSONArray().also { array -> messages.forEach { array.put(it.toJson()) } })
    .put("workflows", JSONArray().also { array -> workflows.forEach { array.put(it.toJson()) } })
    .put("createdAt", createdAt)
    .put("updatedAt", updatedAt)

fun JSONObject.toBotProfile(): BotProfile {
    val connectorArray = optJSONArray("connectorIds") ?: JSONArray()
    val messageArray = optJSONArray("messages") ?: JSONArray()
    val workflowArray = optJSONArray("workflows") ?: JSONArray()
    return BotProfile(
        id = optString("id", UUID.randomUUID().toString()).lowercase(),
        name = optString("name", "Nuevo bot"),
        title = optString("title"),
        description = optString("description"),
        color = optString("color", BOT_COLORS.first()),
        shape = BotShape.entries.firstOrNull { it.name.equals(optString("shape"), true) } ?: BotShape.Bean,
        avatarDataUrl = optString("avatarDataUrl"),
        notificationsEnabled = optBoolean("notificationsEnabled", true),
        connectorIds = List(connectorArray.length()) { connectorArray.getString(it) },
        messages = List(messageArray.length()) { messageArray.getJSONObject(it).toBotMessage() },
        workflows = List(workflowArray.length()) { workflowArray.getJSONObject(it).toBotWorkflow() },
        createdAt = optLong("createdAt", System.currentTimeMillis()),
        updatedAt = optLong("updatedAt", optLong("createdAt", System.currentTimeMillis())),
    )
}

fun BotMessage.toJson() = JSONObject()
    .put("id", id)
    .put("role", role.name.lowercase())
    .put("text", text)
    .put("widget", widget?.toJson() ?: JSONObject.NULL)
    .put("createdAt", createdAt)

fun JSONObject.toBotMessage() = BotMessage(
    id = optString("id", UUID.randomUUID().toString()),
    role = if (optString("role") == "user") MessageRole.User else MessageRole.Assistant,
    text = optString("text"),
    widget = optJSONObject("widget")?.toQuestionWidget(),
    createdAt = optLong("createdAt", System.currentTimeMillis()),
)

fun BotWorkflow.toJson() = JSONObject()
    .put("id", id)
    .put("title", title)
    .put("summary", summary)
    .put("steps", JSONArray(steps))
    .put("recordingId", recordingId)
    .put("recordingMimeType", recordingMimeType)
    .put("createdAt", createdAt)
    .put("updatedAt", updatedAt)
    .put("lastRunAt", lastRunAt ?: JSONObject.NULL)

fun JSONObject.toBotWorkflow(): BotWorkflow {
    val values = optJSONArray("steps") ?: JSONArray()
    return BotWorkflow(
        id = optString("id"),
        title = optString("title"),
        summary = optString("summary"),
        steps = List(values.length()) { values.getString(it) },
        recordingId = optString("recordingId"),
        recordingMimeType = optString("recordingMimeType"),
        createdAt = optLong("createdAt", System.currentTimeMillis()),
        updatedAt = optLong("updatedAt", System.currentTimeMillis()),
        lastRunAt = if (isNull("lastRunAt")) null else optLong("lastRunAt"),
    )
}

fun BotQuestionWidget.toJson() = JSONObject()
    .put("prompt", prompt)
    .put("helpText", helpText)
    .put("options", JSONArray().also { array -> options.forEach { option ->
        array.put(JSONObject().put("label", option.label).put("value", option.value).put("description", option.description))
    } })
    .put("allowCustom", allowCustom)
    .put("dismissOnMoveOn", dismissOnMoveOn)

fun JSONObject.toQuestionWidget(): BotQuestionWidget? {
    val prompt = optString("prompt").trim().take(300)
    val values = optJSONArray("options") ?: return null
    if (prompt.isEmpty() || values.length() !in 1..6) return null
    val options = buildList {
        repeat(values.length().coerceAtMost(6)) { index ->
            val item = values.optJSONObject(index) ?: return@repeat
            val label = item.optString("label").trim().take(120)
            if (label.isNotEmpty()) add(
                BotQuestionOption(
                    label = label,
                    value = item.optString("value", label).ifBlank { label }.take(300),
                    description = item.optString("description").take(240),
                )
            )
        }
    }
    if (options.isEmpty()) return null
    return BotQuestionWidget(
        prompt = prompt,
        helpText = optString("helpText").take(500),
        options = options,
        allowCustom = optBoolean("allowCustom"),
        dismissOnMoveOn = optBoolean("dismissOnMoveOn", true),
    )
}

fun parseAgentAnswer(raw: String): GeneratedAnswer {
    val trimmed = raw.trim().removePrefix("```json").removePrefix("```").removeSuffix("```").trim()
    val parsed = firstAgentEnvelope(trimmed)
        ?: return GeneratedAnswer(raw.trim().take(20_000), null)
    return GeneratedAnswer(
        text = parsed.optString("text").trim().take(20_000),
        widget = parsed.optJSONObject("widget")?.toQuestionWidget(),
    )
}

private fun firstAgentEnvelope(value: String): JSONObject? {
    fun decode(candidate: String): JSONObject? {
        val parsed = runCatching { org.json.JSONTokener(candidate).nextValue() }.getOrNull()
        return when (parsed) {
            is JSONObject -> parsed
            is String -> firstAgentEnvelope(parsed)
            else -> null
        }
    }
    decode(value)?.let { return it }
    var start = -1
    var depth = 0
    var inString = false
    var escaped = false
    value.forEachIndexed { index, character ->
        if (inString) {
            when {
                escaped -> escaped = false
                character == '\\' -> escaped = true
                character == '"' -> inString = false
            }
        } else {
            when (character) {
                '"' -> inString = true
                '{' -> {
                    if (depth == 0) start = index
                    depth += 1
                }
                '}' -> if (depth > 0) {
                    depth -= 1
                    if (depth == 0 && start >= 0) {
                        decode(value.substring(start, index + 1))?.let { return it }
                    }
                }
            }
        }
    }
    return null
}

fun JSONObject.optNullableString(key: String): String? =
    if (isNull(key)) null else optString(key).takeIf { it.isNotBlank() }
