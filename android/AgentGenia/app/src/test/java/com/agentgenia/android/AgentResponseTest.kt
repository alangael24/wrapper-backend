package com.agentgenia.android

import com.agentgenia.android.model.BotProfile
import com.agentgenia.android.model.BotShape
import com.agentgenia.android.model.parseAgentAnswer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentResponseTest {
    @Test
    fun parsesGeneratedWidgetWithoutHardcodedContent() {
        val result = parseAgentAnswer(
            """```json
            {"text":"Hola, soy Atlas.","widget":{"prompt":"¿Qué resolvemos?","helpText":"Elige o escribe","options":[{"label":"Correo","value":"Ayúdame con mi correo","description":"Priorizar mensajes"}],"allowCustom":true,"dismissOnMoveOn":true}}
            ```""".trimIndent()
        )
        assertEquals("Hola, soy Atlas.", result.text)
        assertNotNull(result.widget)
        assertEquals("Ayúdame con mi correo", result.widget?.options?.single()?.value)
        assertTrue(result.widget?.allowCustom == true)
    }

    @Test
    fun treatsPlainTextAsAValidAnswer() {
        val result = parseAgentAnswer("Una respuesta normal")
        assertEquals("Una respuesta normal", result.text)
        assertNull(result.widget)
    }

    @Test
    fun recoversWidgetEnvelopeFromSurroundingText() {
        val result = parseAgentAnswer(
            """Respuesta del modelo:
            ```json
            {"text":"","widget":{"prompt":"¿Qué deseas hacer?","options":[{"label":"Agendar","value":"Agenda un evento"}]}}
            ```""".trimIndent()
        )
        assertEquals("", result.text)
        assertEquals("¿Qué deseas hacer?", result.widget?.prompt)
    }

    @Test
    fun initialPromptRequiresRuntimeGeneration() {
        val prompt = buildBotPrompt(
            BotProfile(name = "Atlas", title = "Investigador", shape = BotShape.Hexagon),
            userText = "",
            initial = true,
        )
        assertTrue(prompt.contains("Genera al vuelo"))
        assertTrue(prompt.contains("no uses una plantilla fija"))
        assertTrue(prompt.contains("Eres Atlas"))
    }

    @Test
    fun freeTierLabelDoesNotSuppressServerAuthorizedBotOnboarding() {
        assertTrue(shouldSendInitialBotMessage("free", BotProfile(name = "Nuevo bot")))
        assertTrue(shouldSendInitialBotMessage("pro", BotProfile(name = "Nuevo bot")))
        assertTrue(!shouldSendInitialBotMessage(
            "free",
            BotProfile(
                name = "Listo",
                messages = listOf(BotMessage(role = MessageRole.Assistant, text = "Hola")),
            ),
        ))
    }

    @Test
    fun promptNamesOnlyTheEffectiveConnectors() {
        val prompt = buildBotPrompt(
            BotProfile(name = "Atlas", connectorIds = listOf("github", "slack")),
            userText = "Revisa mi trabajo",
            initial = false,
        )
        assertTrue(prompt.contains("Conectores autorizables: github, slack"))
        assertTrue(prompt.endsWith("Usuario: Revisa mi trabajo"))
    }
}
