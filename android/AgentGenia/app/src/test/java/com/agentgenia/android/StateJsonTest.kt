package com.agentgenia.android

import com.agentgenia.android.model.BotMessage
import com.agentgenia.android.model.BotProfile
import com.agentgenia.android.model.MessageRole
import com.agentgenia.android.model.PersistedAccountState
import com.agentgenia.android.model.toJson
import com.agentgenia.android.model.toPersistedAccountState
import org.junit.Assert.assertEquals
import org.junit.Test

class StateJsonTest {
    @Test
    fun accountStateRoundTripsWithoutLosingBotData() {
        val bot = BotProfile(
            id = "bot-1",
            name = "Analista",
            connectorIds = listOf("github", "slack"),
            messages = listOf(BotMessage(id = "message-1", role = MessageRole.User, text = "Hola")),
        )
        val original = PersistedAccountState(listOf(bot), listOf("github"), "bot-1")
        val restored = original.toJson().toPersistedAccountState()
        assertEquals(original, restored)
    }
}
