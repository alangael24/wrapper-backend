package com.agentgenia.android.data

import android.content.Context
import android.util.Base64
import com.agentgenia.android.model.AccountSession
import com.agentgenia.android.model.PersistedAccountState
import com.agentgenia.android.model.toAccountSession
import com.agentgenia.android.model.toJson
import com.agentgenia.android.model.toPersistedAccountState
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import androidx.core.content.edit

class SecureStore(private val context: Context) {
    private val preferences = context.getSharedPreferences("agentgenia.secure", Context.MODE_PRIVATE)

    @Synchronized
    fun readSession(): AccountSession? {
        val encoded = preferences.getString(SESSION_KEY, null) ?: return null
        return runCatching {
            val data = decrypt(Base64.decode(encoded, Base64.NO_WRAP), SESSION_AAD)
            JSONObject(data.decodeToString()).toAccountSession()
        }.getOrNull()
    }

    @Synchronized
    fun writeSession(session: AccountSession) {
        val encrypted = encrypt(session.toJson().toString().encodeToByteArray(), SESSION_AAD)
        preferences.edit { putString(SESSION_KEY, Base64.encodeToString(encrypted, Base64.NO_WRAP)) }
    }

    @Synchronized
    fun clearSession() {
        preferences.edit { remove(SESSION_KEY) }
    }

    @Synchronized
    fun clearAfterAccountDeletion() {
        preferences.edit {
            remove(SESSION_KEY)
            remove(DEVICE_KEY)
        }
    }

    fun deviceId(): String {
        preferences.getString(DEVICE_KEY, null)?.let { return it }
        val generated = java.util.UUID.randomUUID().toString().lowercase()
        preferences.edit { putString(DEVICE_KEY, generated) }
        return generated
    }

    fun readAccountState(accountId: String): PersistedAccountState {
        val file = accountFile(accountId)
        if (!file.exists()) return PersistedAccountState()
        return runCatching {
            val encrypted = file.readBytes()
            JSONObject(decrypt(encrypted, accountAad(accountId)).decodeToString()).toPersistedAccountState()
        }.getOrElse { PersistedAccountState() }
    }

    fun writeAccountState(accountId: String, state: PersistedAccountState) {
        val file = accountFile(accountId)
        file.parentFile?.mkdirs()
        val temporary = File(file.parentFile, "${file.name}.tmp")
        val encrypted = encrypt(state.toJson().toString().encodeToByteArray(), accountAad(accountId))
        temporary.writeBytes(encrypted)
        if (!temporary.renameTo(file)) {
            file.writeBytes(encrypted)
            temporary.delete()
        }
    }

    fun deleteAccountState(accountId: String) {
        val file = accountFile(accountId)
        check(!file.exists() || file.delete()) { "No fue posible eliminar los datos locales de la cuenta." }
    }

    private fun accountFile(accountId: String): File {
        val hash = MessageDigest.getInstance("SHA-256")
            .digest(accountId.encodeToByteArray())
            .joinToString("") { "%02x".format(it) }
        return File(context.noBackupFilesDir, "accounts/$hash.bin")
    }

    private fun accountAad(accountId: String) = "agentgenia.account.$accountId".encodeToByteArray()

    private fun encrypt(cleartext: ByteArray, aad: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        cipher.updateAAD(aad)
        return cipher.iv + cipher.doFinal(cleartext)
    }

    private fun decrypt(payload: ByteArray, aad: ByteArray): ByteArray {
        require(payload.size > IV_BYTES)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, payload.copyOfRange(0, IV_BYTES)))
        cipher.updateAAD(aad)
        return cipher.doFinal(payload.copyOfRange(IV_BYTES, payload.size))
    }

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }

    private companion object {
        const val KEY_ALIAS = "com.agentgenia.android.secure.v1"
        const val SESSION_KEY = "session.v1"
        const val DEVICE_KEY = "device.id"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
        val SESSION_AAD = "agentgenia.session.v1".encodeToByteArray()
    }
}
