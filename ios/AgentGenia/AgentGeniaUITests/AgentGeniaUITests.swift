import XCTest

final class AgentGeniaUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testAppLaunchesIntoAnInteractiveAccountState() {
        let app = XCUIApplication()
        app.launchArguments += ["-ui-testing"]
        app.launch()

        let signedOut = app.buttons["Continuar con Google"]
        let signedIn = app.navigationBars["Agent Genia"]
        XCTAssertTrue(
            signedOut.waitForExistence(timeout: 10) || signedIn.waitForExistence(timeout: 2),
            "The application never reached its signed-in or signed-out shell"
        )
    }
}
