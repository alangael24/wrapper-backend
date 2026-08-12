package com.agentgenia.android.data

import com.agentgenia.android.BuildConfig
import com.agentgenia.android.model.AccountIdentity
import com.agentgenia.android.model.AccountProfile
import com.agentgenia.android.model.AccountSession
import com.agentgenia.android.model.BillingPlan
import com.agentgenia.android.model.BillingSnapshot
import com.agentgenia.android.model.BillingSubscription
import com.agentgenia.android.model.ComputerSnapshot
import com.agentgenia.android.model.ComputerState
import com.agentgenia.android.model.ConnectorStatus
import com.agentgenia.android.model.optNullableString
import com.agentgenia.android.model.toAccountIdentity
import com.agentgenia.android.model.toAccountSession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.util.concurrent.TimeUnit

data class AuthStart(val attemptId: String, val authorizeUrl: String, val expiresIn: Int)
data class AuthStatus(
    val status: String,
    val message: String?,
    val token: String?,
    val refreshToken: String?,
    val expiresAt: Long?,
    val account: AccountIdentity?,
)
data class ConnectorStart(val attemptId: String, val authorizeUrl: String)
data class ConnectorPoll(val status: String, val message: String?)

class ServiceException(
    override val message: String,
    val code: String,
    val status: Int,
) : Exception(message)

class AgentGeniaApi(
    private val secureStore: SecureStore,
    baseUrlValue: String = BuildConfig.API_BASE_URL,
) {
    private val baseUrl = validateBaseUrl(baseUrlValue)
    private val jsonType = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .callTimeout(190, TimeUnit.SECONDS)
        .build()
    private val sessionMutex = Mutex()
    @Volatile private var session: AccountSession? = null

    fun restoreSession(): AccountSession? = secureStore.readSession().also { session = it }

    suspend fun beginSignIn(appVersion: String): AuthStart {
        val json = requestJson(
            "/v1/account-auth/start",
            "POST",
            JSONObject().put("device_id", secureStore.deviceId()).put("app_version", appVersion),
            authorized = false,
        )
        return AuthStart(json.getString("attempt_id"), json.getString("authorize_url"), json.optInt("expires_in"))
    }

    suspend fun authStatus(attemptId: String): AuthStatus {
        val path = "/v1/account-auth/status/${path(attemptId)}?device_id=${query(secureStore.deviceId())}"
        val json = requestJson(path, authorized = false)
        return AuthStatus(
            status = json.optString("status"),
            message = json.optNullableString("message"),
            token = json.optNullableString("token"),
            refreshToken = json.optNullableString("refresh_token"),
            expiresAt = if (json.has("expires_at") && !json.isNull("expires_at")) json.optLong("expires_at") else null,
            account = json.optJSONObject("account")?.toAccountIdentity(),
        )
    }

    fun completeSignIn(status: AuthStatus): AccountSession {
        val completed = AccountSession(
            token = status.token ?: throw ServiceException("El servidor devolvió una sesión incompleta.", "invalid_session", 502),
            refreshToken = status.refreshToken ?: throw ServiceException("El servidor devolvió una sesión incompleta.", "invalid_session", 502),
            expiresAt = status.expiresAt ?: throw ServiceException("El servidor devolvió una sesión incompleta.", "invalid_session", 502),
            account = status.account ?: throw ServiceException("El servidor devolvió una sesión incompleta.", "invalid_session", 502),
        )
        secureStore.writeSession(completed)
        session = completed
        return completed
    }

    suspend fun signOut() {
        val current = session ?: secureStore.readSession()
        if (current != null) runCatching {
            requestJson("/v1/account-auth/logout", "POST", JSONObject(), authorized = false, token = current.token)
        }
        session = null
        secureStore.clearSession()
    }

    suspend fun me(): AccountProfile {
        val json = requestJson("/v1/me")
        return AccountProfile(
            userId = json.optString("user_id"), name = json.optString("name"), email = json.optString("email"),
            tier = json.optString("tier"), tierLabel = json.optString("tier_label"),
        )
    }

    suspend fun connectors(): List<ConnectorStatus> {
        val array = requestJson("/v1/connectors").optJSONArray("connectors") ?: JSONArray()
        return List(array.length()) { index ->
            val item = array.getJSONObject(index)
            ConnectorStatus(
                connectorId = item.optString("connector_id"),
                provider = item.optNullableString("provider"),
                available = item.optBoolean("available"),
                connected = item.optBoolean("connected"),
                account = item.optString("account"),
                reason = item.optString("reason"),
            )
        }
    }

    suspend fun startConnector(connectorId: String): ConnectorStart {
        val json = requestJson(
            "/v1/connectors/start", "POST", JSONObject().put("connector_id", connectorId),
        )
        return ConnectorStart(json.getString("attempt_id"), json.getString("authorize_url"))
    }

    suspend fun connectorStatus(attemptId: String): ConnectorPoll {
        val json = requestJson("/v1/connectors/status/${path(attemptId)}")
        return ConnectorPoll(json.optString("status"), json.optNullableString("message"))
    }

    suspend fun disconnectConnector(connectorId: String) {
        requestJson("/v1/connectors/disconnect", "POST", JSONObject().put("connector_id", connectorId))
    }

    suspend fun runAgent(prompt: String, botId: String, connectorIds: List<String>): String {
        val json = requestJson(
            "/v1/agent/run",
            "POST",
            JSONObject()
                .put("prompt", prompt)
                .put("browser", false)
                .put("computer", true)
                .put("bot_id", botId)
                .put("connector_ids", JSONArray(connectorIds)),
        )
        return json.optString("answer")
    }

    suspend fun computerStatus(botId: String): ComputerSnapshot =
        requestJson("/v1/computers/${path(botId)}").toComputerSnapshot()

    suspend fun ensureComputer(botId: String, botName: String): ComputerSnapshot =
        requestJson(
            "/v1/computers/${path(botId)}/ensure", "POST", JSONObject().put("bot_name", botName),
        ).toComputerSnapshot()

    suspend fun handBackComputer(botId: String): ComputerSnapshot =
        requestJson("/v1/computers/${path(botId)}/hand-back", "POST", JSONObject()).toComputerSnapshot()

    suspend fun billing(): BillingSnapshot {
        val json = requestJson("/v1/billing")
        val planJson = json.optJSONObject("plans") ?: JSONObject()
        val plans = buildMap {
            listOf("basic", "pro").forEach { id ->
                planJson.optJSONObject(id)?.let { value ->
                    put(id, BillingPlan(
                        name = value.optString("name", id.replaceFirstChar { it.uppercase() }),
                        amount = value.optInt("amount"),
                        currency = value.optString("currency", "usd"),
                        interval = value.optString("interval", "month"),
                    ))
                }
            }
        }
        val subscription = json.optJSONObject("subscription")?.let { value ->
            BillingSubscription(
                status = value.optString("status"),
                tier = value.optString("tier"),
                cancelAtPeriodEnd = value.optBoolean("cancel_at_period_end"),
                currentPeriodEnd = if (value.isNull("current_period_end")) null else value.optLong("current_period_end"),
            )
        }
        return BillingSnapshot(
            configured = json.optBoolean("configured"),
            tier = json.optString("tier", "free"),
            customer = json.optBoolean("customer"),
            subscription = subscription,
            plans = plans,
        )
    }

    suspend fun checkoutUrl(tier: String): String {
        val url = requestJson("/v1/billing/checkout", "POST", JSONObject().put("tier", tier)).getString("checkout_url")
        return validateExternalUrl(url, setOf("checkout.stripe.com"))
    }

    suspend fun portalUrl(): String {
        val url = requestJson("/v1/billing/portal", "POST", JSONObject()).getString("portal_url")
        return validateExternalUrl(url, setOf("billing.stripe.com"))
    }

    fun validateAuthorizationUrl(url: String, googleOnly: Boolean = false): String =
        validateExternalUrl(url, if (googleOnly) setOf("accounts.google.com") else null)

    private suspend fun requestJson(
        path: String,
        method: String = "GET",
        body: JSONObject? = null,
        authorized: Boolean = true,
        token: String? = null,
    ): JSONObject = withContext(Dispatchers.IO) {
        val authorization = token ?: if (authorized) accessToken() else null
        val response = perform(path, method, body, authorization)
        if (response.status == 401 && authorized) {
            val refreshed = refreshSession()
            val retry = perform(path, method, body, refreshed.token)
            decode(retry)
        } else decode(response)
    }

    private data class RawResponse(val status: Int, val body: String)

    private fun perform(path: String, method: String, body: JSONObject?, token: String?): RawResponse {
        val url = baseUrl.resolve(path) ?: throw ServiceException("Ruta inválida.", "invalid_url", 500)
        val builder = Request.Builder().url(url).header("Accept", "application/json")
        if (token != null) builder.header("Authorization", "Bearer $token")
        when (method) {
            "GET" -> builder.get()
            "POST" -> builder.post((body ?: JSONObject()).toString().toRequestBody(jsonType))
            else -> throw ServiceException("Método inválido.", "invalid_method", 500)
        }
        try {
            client.newCall(builder.build()).execute().use { response ->
                return RawResponse(response.code, response.body.string())
            }
        } catch (error: ServiceException) {
            throw error
        } catch (_: Exception) {
            throw ServiceException("No fue posible conectar con Agent Genia.", "network_error", 0)
        }
    }

    private fun decode(response: RawResponse): JSONObject {
        val json = runCatching { if (response.body.isBlank()) JSONObject() else JSONObject(response.body) }.getOrElse {
            if (response.status in 200..299) throw ServiceException("El servidor devolvió una respuesta incompatible.", "invalid_response", 502)
            JSONObject()
        }
        if (response.status !in 200..299) {
            val error = json.optJSONObject("error")
            throw ServiceException(
                error?.optString("message")?.takeIf { it.isNotBlank() } ?: "Agent Genia respondió HTTP ${response.status}.",
                error?.optString("type")?.takeIf { it.isNotBlank() } ?: "http_error",
                response.status,
            )
        }
        return json
    }

    private suspend fun accessToken(): String = sessionMutex.withLock {
        val current = session ?: secureStore.readSession()
            ?: throw ServiceException("Primero inicia sesión en Agent Genia.", "account_required", 401)
        session = current
        if (current.expiresAt - 60_000 > System.currentTimeMillis()) current.token else refreshSessionUnlocked().token
    }

    private suspend fun refreshSession(): AccountSession = sessionMutex.withLock { refreshSessionUnlocked() }

    private fun refreshSessionUnlocked(): AccountSession {
        val current = session ?: secureStore.readSession()
            ?: throw ServiceException("Tu sesión expiró. Inicia sesión nuevamente.", "account_required", 401)
        val response = perform(
            "/v1/account-auth/refresh",
            "POST",
            JSONObject().put("device_id", secureStore.deviceId()),
            current.refreshToken,
        )
        val refreshed = runCatching { decode(response).toAccountSession() }.getOrElse {
            session = null
            secureStore.clearSession()
            throw ServiceException("Tu sesión expiró. Inicia sesión nuevamente.", "account_required", 401)
        }
        session = refreshed
        secureStore.writeSession(refreshed)
        return refreshed
    }

    private fun JSONObject.toComputerSnapshot(): ComputerSnapshot {
        val state = ComputerState.entries.firstOrNull { it.name.equals(optString("state"), true) } ?: ComputerState.Error
        return ComputerSnapshot(
            configured = optBoolean("configured"), botId = optString("bot_id"), provider = optNullableString("provider"),
            state = state, viewerUrl = optString("viewer_url"), viewerExpiresAt = optLong("viewer_expires_at"),
            reason = optString("reason"),
        )
    }

    private fun validateBaseUrl(value: String): okhttp3.HttpUrl {
        val uri = runCatching { URI(value) }.getOrNull()
            ?: throw IllegalArgumentException("API_BASE_URL inválida")
        val local = uri.scheme == "http" && uri.host in setOf("localhost", "127.0.0.1", "::1")
        require((uri.scheme == "https" || local) && uri.userInfo == null) { "API_BASE_URL debe ser HTTPS o loopback" }
        return okhttp3.HttpUrl.Builder()
            .scheme(uri.scheme).host(uri.host).apply { if (uri.port != -1) port(uri.port) }.build()
    }

    private fun validateExternalUrl(value: String, hosts: Set<String>?): String {
        val uri = runCatching { URI(value) }.getOrNull()
            ?: throw ServiceException("El servidor devolvió una URL no segura.", "unsafe_url", 502)
        if (uri.scheme != "https" || uri.userInfo != null || uri.host.isNullOrBlank() || (hosts != null && uri.host !in hosts)) {
            throw ServiceException("El servidor devolvió una URL no segura.", "unsafe_url", 502)
        }
        return uri.toASCIIString()
    }

    private fun path(value: String) = java.net.URLEncoder.encode(value, Charsets.UTF_8.name()).replace("+", "%20")
    private fun query(value: String) = path(value)
}
