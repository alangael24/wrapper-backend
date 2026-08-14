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

    func testConnectorCatalogUsesUniqueIdentifiers() {
        let identifiers = ConnectorDefinition.catalog.map(\.id)
        XCTAssertEqual(Set(identifiers).count, identifiers.count)
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
}
