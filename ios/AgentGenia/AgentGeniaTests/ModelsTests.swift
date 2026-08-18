import XCTest
@testable import Agent_Genia

final class ModelsTests: XCTestCase {
    func testAccountSessionRoundTripPreservesRotatingCredentials() throws {
        let identity = AccountIdentity(id: "user-1", email: "alan@example.com", name: "Alan", picture: "")
        let original = AccountSession(token: "access", refreshToken: "refresh", expiresAt: 123_456, account: identity)
        let restored = try JSONDecoder().decode(AccountSession.self, from: JSONEncoder().encode(original))
        XCTAssertEqual(restored, original)
    }

    func testPersistedBotsRemainIsolatedWithinTheirAccountState() throws {
        let bot = BotProfile(
            id: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!,
            name: "Analista",
            title: "Investigación",
            description: "Resume información",
            color: "#2f91f5",
            shape: .bean,
            notificationsEnabled: true,
            connectorIDs: ["github", "notion"],
            messages: [],
            createdAt: Date(timeIntervalSince1970: 1_700_000_000)
        )
        let state = PersistedAccountState(bots: [bot], selectedConnectorIDs: ["github"], activeBotID: bot.id)
        let restored = try JSONDecoder().decode(PersistedAccountState.self, from: JSONEncoder().encode(state))
        XCTAssertEqual(restored, state)
    }

    func testFreeTierLabelDoesNotSuppressServerAuthorizedBotOnboarding() {
        let emptyBot = BotProfile(
            id: UUID(), name: "Nuevo bot", title: "", description: "",
            color: "#2f91f5", shape: .circle, notificationsEnabled: true,
            connectorIDs: [], messages: [], createdAt: Date()
        )
        XCTAssertTrue(shouldSendInitialBotMessage(tier: "free", bot: emptyBot))
        XCTAssertTrue(shouldSendInitialBotMessage(tier: "pro", bot: emptyBot))

        let answeredBot = BotProfile(
            id: UUID(), name: "Listo", title: "", description: "",
            color: "#2f91f5", shape: .circle, notificationsEnabled: true,
            connectorIDs: [],
            messages: [
                BotMessage(
                    id: UUID(), role: .assistant, text: "Hola", widget: nil,
                    createdAt: Date()
                )
            ],
            createdAt: Date()
        )
        XCTAssertFalse(shouldSendInitialBotMessage(tier: "free", bot: answeredBot))
    }

    func testCompletedReplySurvivesMissingThinkingPlaceholder() throws {
        let botID = UUID(uuidString: "00000000-0000-0000-0000-000000000002")!
        let replyID = UUID(uuidString: "00000000-0000-0000-0000-000000000003")!
        let createdAt = Date(timeIntervalSince1970: 1_700_000_010)
        var bots = [BotProfile(
            id: botID,
            name: "Nuevo bot",
            title: "",
            description: "",
            color: "#2f91f5",
            shape: .circle,
            notificationsEnabled: true,
            connectorIDs: [],
            messages: [],
            createdAt: Date(timeIntervalSince1970: 1_700_000_000)
        )]

        XCTAssertTrue(installAgentReply(
            in: &bots,
            botID: botID,
            messageID: replyID,
            text: "  Respuesta recuperada  ",
            widget: nil,
            createdAt: createdAt
        ))
        let message = try XCTUnwrap(bots.first?.messages.first)
        XCTAssertEqual(message.id, replyID)
        XCTAssertEqual(message.text, "Respuesta recuperada")
        XCTAssertEqual(message.createdAt, createdAt)
    }

    func testCompletedReplyReplacesThinkingPlaceholderWithoutDuplication() throws {
        let botID = UUID(uuidString: "00000000-0000-0000-0000-000000000004")!
        let replyID = UUID(uuidString: "00000000-0000-0000-0000-000000000005")!
        let createdAt = Date(timeIntervalSince1970: 1_700_000_020)
        var bots = [BotProfile(
            id: botID,
            name: "Nuevo bot",
            title: "",
            description: "",
            color: "#2f91f5",
            shape: .circle,
            notificationsEnabled: true,
            connectorIDs: [],
            messages: [BotMessage(
                id: replyID,
                role: .assistant,
                text: "",
                widget: nil,
                createdAt: createdAt
            )],
            createdAt: Date(timeIntervalSince1970: 1_700_000_000)
        )]

        XCTAssertTrue(installAgentReply(
            in: &bots,
            botID: botID,
            messageID: replyID,
            text: "Lista",
            widget: nil,
            createdAt: Date()
        ))
        XCTAssertEqual(bots[0].messages.count, 1)
        XCTAssertEqual(bots[0].messages[0].text, "Lista")
        XCTAssertEqual(bots[0].messages[0].createdAt, createdAt)
    }

    func testDurableRunUsesTheSameAssistantMessageIDOnEveryReplay() {
        XCTAssertEqual(
            assistantMessageID(idempotencyKey: "turn-123").uuidString.lowercased(),
            "6a5b1dc6-534d-50b9-a858-dc91a382de41"
        )
        XCTAssertNotEqual(
            assistantMessageID(idempotencyKey: "turn-123"),
            assistantMessageID(idempotencyKey: "turn-124")
        )
    }

    func testDuplicateAssistantReplyIsCollapsedOnlyWithinItsUserTurn() {
        let firstUser = BotMessage(id: UUID(), role: .user, text: "Hola", widget: nil, createdAt: Date())
        let firstReply = BotMessage(id: UUID(), role: .assistant, text: "Listo", widget: nil, createdAt: Date())
        let duplicateReply = BotMessage(id: UUID(), role: .assistant, text: "Listo", widget: nil, createdAt: Date())
        let secondUser = BotMessage(id: UUID(), role: .user, text: "Otra vez", widget: nil, createdAt: Date())
        let legitimateRepeat = BotMessage(id: UUID(), role: .assistant, text: "Listo", widget: nil, createdAt: Date())

        let messages = deduplicatedConversationMessages([
            firstUser, firstReply, duplicateReply, secondUser, legitimateRepeat
        ])

        XCTAssertEqual(messages.map(\.id), [
            firstUser.id, firstReply.id, secondUser.id, legitimateRepeat.id
        ])
    }

    func testAgentEnvelopeIsRecoveredFromSurroundingModelText() throws {
        let generated = parseAgentAnswer("""
        Aquí está la pregunta:
        ```json
        {"text":"Vamos a programarlo.","widget":{"prompt":"¿Qué título le pongo?","options":[{"label":"Inicio de trabajo","value":"Ponle Inicio de trabajo"}],"allowCustom":true}}
        ```
        """)

        XCTAssertEqual(generated.text, "Vamos a programarlo.")
        XCTAssertEqual(generated.widget?.prompt, "¿Qué título le pongo?")
        XCTAssertEqual(generated.widget?.options.first?.value, "Ponle Inicio de trabajo")
    }

    func testPartialAgentEnvelopeShowsOnlyCompletedVisibleText() {
        let generated = parseAgentAnswer(
            #"{"text":"¡Hola! Soy Nuevo bot, agente de Agent Genia.","widget":{"prompt":"¿Qué deseas lograr?","help"#
        )

        XCTAssertEqual(generated.text, "¡Hola! Soy Nuevo bot, agente de Agent Genia.")
        XCTAssertNil(generated.widget)
    }

    func testPersistedPartialEnvelopeIsSanitizedWhenDecoded() throws {
        let id = UUID()
        let stored = """
        {"id":"\(id.uuidString)","role":"assistant","text":"{\\\"text\\\":\\\"Hola visible\\\",\\\"widget\\\":{" ,"widget":null,"createdAt":0}
        """
        let message = try JSONDecoder().decode(BotMessage.self, from: Data(stored.utf8))

        XCTAssertEqual(message.text, "Hola visible")
        XCTAssertNil(message.widget)
    }

    func testWidgetOnlyReplyReplacesThinkingPlaceholder() throws {
        let botID = UUID()
        let replyID = UUID()
        let widget = try XCTUnwrap(parseAgentAnswer("""
        {"text":"","widget":{"prompt":"¿Qué deseas hacer?","options":[{"label":"Agendar","value":"Agenda un evento"}]}}
        """).widget)
        var bots = [BotProfile(
            id: botID, name: "Nuevo bot", title: "", description: "",
            color: "#2f91f5", shape: .circle, notificationsEnabled: true,
            connectorIDs: [], messages: [], createdAt: Date()
        )]

        XCTAssertTrue(installAgentReply(
            in: &bots,
            botID: botID,
            messageID: replyID,
            text: "",
            widget: widget,
            createdAt: Date()
        ))
        XCTAssertEqual(bots[0].messages.first?.widget?.prompt, "¿Qué deseas hacer?")
    }

    func testConnectorCatalogUsesUniqueIdentifiers() {
        let identifiers = ConnectorDefinition.catalog.map(\.id)
        XCTAssertEqual(Set(identifiers).count, identifiers.count)
    }

    func testRoutingContextIsCompactAndContainsOnlyRecentConversation() {
        let messages = (0..<7).map { index in
            BotMessage(
                id: UUID(),
                role: index.isMultiple(of: 2) ? .user : .assistant,
                text: String(repeating: "x", count: 1_500) + "-\(index)",
                widget: nil,
                createdAt: Date(timeIntervalSince1970: TimeInterval(index))
            )
        }
        let bot = BotProfile(
            id: UUID(), name: "Agente", title: "Calendario", description: "Privado",
            color: "#2f91f5", shape: .circle, notificationsEnabled: true,
            connectorIDs: ["google-workspace"], messages: messages, createdAt: Date()
        )

        let context = buildRoutingContext(bot: bot)

        XCTAssertLessThanOrEqual(context.count, 4_100)
        XCTAssertFalse(context.contains("Calendario"))
        XCTAssertFalse(context.contains("google-workspace"))
        XCTAssertFalse(context.contains("-2"))
        XCTAssertTrue(context.contains("Usuario:"))
        XCTAssertTrue(context.contains("Agente:"))
    }

    func testServerSentEventParserFlushesDoneFrameAtEOF() throws {
        var parser = ServerSentEventParser()
        XCTAssertNil(parser.consume(line: "event: done"))
        XCTAssertNil(parser.consume(line: "data: {\"answer\":\"hola\"}"))

        let frame = try XCTUnwrap(parser.finish())
        XCTAssertEqual(frame.name, "done")
        XCTAssertEqual(String(decoding: frame.data, as: UTF8.self), "{\"answer\":\"hola\"}")
        XCTAssertNil(parser.finish())
    }

    func testServerSentEventParserIgnoresHeartbeatAndEmitsOnBlankLine() throws {
        var parser = ServerSentEventParser()
        XCTAssertNil(parser.consume(line: ": keep-alive"))
        XCTAssertNil(parser.consume(line: "event: delta"))
        XCTAssertNil(parser.consume(line: "data: {\"text\":\"hola\"}"))

        let frame = try XCTUnwrap(parser.consume(line: ""))
        XCTAssertEqual(frame.name, "delta")
        XCTAssertEqual(String(decoding: frame.data, as: UTF8.self), "{\"text\":\"hola\"}")
    }

    func testServerSentEventParserUsesNextEventAsBoundaryWithoutBlankLine() throws {
        var parser = ServerSentEventParser()
        XCTAssertNil(parser.consume(line: "event: start"))
        XCTAssertNil(parser.consume(line: "data: {\"run_id\":\"run-1\"}"))

        let start = try XCTUnwrap(parser.consume(line: "event: done64"))
        XCTAssertEqual(start.name, "start")
        XCTAssertEqual(String(decoding: start.data, as: UTF8.self), "{\"run_id\":\"run-1\"}")

        XCTAssertNil(parser.consume(line: "data: aG9sYQ=="))
        let done = try XCTUnwrap(parser.finish())
        XCTAssertEqual(done.name, "done64")
        XCTAssertEqual(String(decoding: done.data, as: UTF8.self), "aG9sYQ==")
    }

    func testServerSentEventParserAcceptsCarriageReturnBlankLine() throws {
        var parser = ServerSentEventParser()
        XCTAssertNil(parser.consume(line: "event: delta\r"))
        XCTAssertNil(parser.consume(line: "data: {\"text\":\"hola\"}\r"))

        let frame = try XCTUnwrap(parser.consume(line: "\r"))
        XCTAssertEqual(frame.name, "delta")
        XCTAssertEqual(String(decoding: frame.data, as: UTF8.self), "{\"text\":\"hola\"}")
    }
}
