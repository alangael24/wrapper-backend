package com.agentgenia.android

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.agentgenia.android.data.SecureStore
import com.agentgenia.android.model.AccountIdentity
import com.agentgenia.android.model.AccountSession
import com.agentgenia.android.model.BotProfile
import com.agentgenia.android.model.PersistedAccountState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class SecureStoreInstrumentedTest {
    @Test
    fun sessionAndAccountStateRoundTripThroughAndroidKeystore() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val store = SecureStore(context)
        store.clearSession()
        val account = AccountIdentity("instrumented-user", "test@example.com", "Test", "")
        val session = AccountSession("access", "refresh", 123_456L, account)
        store.writeSession(session)
        assertEquals(session, store.readSession())

        val bot = BotProfile(id = UUID.randomUUID().toString(), name = "Prueba")
        val state = PersistedAccountState(listOf(bot), emptyList(), bot.id)
        store.writeAccountState(account.id, state)
        assertEquals(state, store.readAccountState(account.id))

        store.clearSession()
        assertNull(store.readSession())
    }
}
