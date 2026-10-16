import Darwin
import XCTest

@testable import Jobtrail

/// These start real (harmless) processes, so they exercise the actual start, health-wait,
/// crash and shutdown paths rather than a mock of them.
@MainActor
final class ServerLauncherTests: XCTestCase {
    private var root: URL!

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func config(
        _ executable: String, _ arguments: [String], timeout: TimeInterval = 10
    ) -> ServerConfig {
        ServerConfig(
            repoURL: root,
            executableURL: URL(fileURLWithPath: executable),
            arguments: arguments,
            logURL: root.appendingPathComponent("logs/server.log"),
            startupTimeout: timeout
        )
    }

    private func launcher(
        _ config: ServerConfig, healthy: @escaping @Sendable (URL) async -> Bool = { _ in true }
    ) -> ServerLauncher {
        ServerLauncher(configProvider: { config }, healthCheck: healthy)
    }

    private func isAlive(_ pid: Int32) -> Bool { kill(pid, 0) == 0 }

    private func waitUntil(_ condition: () -> Bool, seconds: Double = 3) async {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition(), Date() < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    func testStartsTheServerAndStopsItOnShutdown() async throws {
        let launcher = launcher(config("/bin/sleep", ["60"]))
        await launcher.start()

        guard case .running(let url) = launcher.state else {
            return XCTFail("expected running, got \(launcher.state)")
        }
        XCTAssertEqual(url.host, "127.0.0.1")
        XCTAssertGreaterThan(url.port ?? 0, 1024)
        let pid = try XCTUnwrap(launcher.processIdentifier)
        XCTAssertTrue(isAlive(pid))

        launcher.shutdown()
        await waitUntil { !self.isAlive(pid) }
        XCTAssertFalse(isAlive(pid), "the server must not outlive the app")
    }

    func testTheServerGetsItsPortAndTheAppsPid() async throws {
        let launcher = launcher(config("/bin/sh", ["-c", "env; sleep 60"]))
        await launcher.start()
        guard case .running(let url) = launcher.state else { return XCTFail("\(launcher.state)") }
        let logURL = try XCTUnwrap(launcher.config?.logURL)

        await waitUntil { !ServerLauncher.logTail(at: logURL, lines: 200).isEmpty }
        let log = ServerLauncher.logTail(at: logURL, lines: 200)
        XCTAssertTrue(log.contains("JOBTRAIL_PORT=\(url.port!)"))
        XCTAssertTrue(log.contains("JOBTRAIL_PARENT_PID=\(ProcessInfo.processInfo.processIdentifier)"))
        launcher.shutdown()
    }

    func testAServerThatDiesImmediatelyReportsItWithItsLog() async {
        // A real server that died would not answer the health check either.
        let launcher = launcher(
            config("/bin/sh", ["-c", "echo boom-from-the-server; exit 3"]), healthy: { _ in false })
        await launcher.start()

        guard case .failed(let message) = launcher.state else { return XCTFail("\(launcher.state)") }
        XCTAssertTrue(message.contains("exit code 3"))
        XCTAssertTrue(message.contains("boom-from-the-server"), "the log tail should be included")
    }

    func testAServerThatNeverAnswersTimesOutAndIsStopped() async throws {
        let launcher = launcher(config("/bin/sleep", ["60"], timeout: 0.7), healthy: { _ in false })
        await launcher.start()

        guard case .failed(let message) = launcher.state else { return XCTFail("\(launcher.state)") }
        XCTAssertTrue(message.contains("did not answer"))
        XCTAssertNil(launcher.processIdentifier, "the stuck process should have been stopped")
    }

    func testAConfigProblemShowsAnActionableMessage() async {
        let launcher = ServerLauncher(configProvider: { throw ServerConfig.Problem.serverNotInstalled("/x") })
        await launcher.start()
        guard case .failed(let message) = launcher.state else { return XCTFail("\(launcher.state)") }
        XCTAssertTrue(message.contains("uv sync"))
    }

    func testRetryAfterFixingTheProblemWorks() async {
        nonisolated(unsafe) var attempts = 0
        let good = config("/bin/sleep", ["60"])
        let launcher = ServerLauncher(
            configProvider: {
                attempts += 1
                if attempts == 1 { throw ServerConfig.Problem.serverNotInstalled("/x") }
                return good
            },
            healthCheck: { _ in true })

        await launcher.start()
        XCTAssertEqual(launcher.state, .failed(ServerConfig.Problem.serverNotInstalled("/x").localizedDescription))
        await launcher.restart()
        guard case .running = launcher.state else { return XCTFail("\(launcher.state)") }
        launcher.shutdown()
    }

    func testRestartReplacesTheProcess() async throws {
        let launcher = launcher(config("/bin/sleep", ["60"]))
        await launcher.start()
        let first = try XCTUnwrap(launcher.processIdentifier)

        await launcher.restart()
        let second = try XCTUnwrap(launcher.processIdentifier)
        XCTAssertNotEqual(first, second)
        await waitUntil { !self.isAlive(first) }
        XCTAssertFalse(isAlive(first))
        XCTAssertTrue(isAlive(second))
        launcher.shutdown()
    }

    func testAServerThatCrashesLaterIsReported() async throws {
        let launcher = launcher(config("/bin/sleep", ["60"]))
        await launcher.start()
        let pid = try XCTUnwrap(launcher.processIdentifier)

        kill(pid, SIGKILL)  // simulate the server dying on its own
        await waitUntil {
            if case .failed = launcher.state { return true } else { return false }
        }
        guard case .failed(let message) = launcher.state else { return XCTFail("\(launcher.state)") }
        XCTAssertTrue(message.contains("stopped unexpectedly"))
    }

    func testStoppingOnPurposeIsNotReportedAsACrash() async throws {
        let launcher = launcher(config("/bin/sleep", ["60"]))
        await launcher.start()
        launcher.shutdown()
        try await Task.sleep(for: .milliseconds(300))
        guard case .running = launcher.state else { return XCTFail("\(launcher.state)") }
    }

    func testTheHealthCheckIsFalseWhenNothingListens() async throws {
        let port = try FreePort.find()  // free, so nothing is listening on it
        let healthy = await ServerLauncher.defaultHealthCheck(URL(string: "http://127.0.0.1:\(port)")!)
        XCTAssertFalse(healthy)
    }

    func testLogTailKeepsTheLastLines() throws {
        let url = root.appendingPathComponent("tail.log")
        try (1...30).map { "line \($0)" }.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
        let tail = ServerLauncher.logTail(at: url, lines: 3)
        XCTAssertEqual(tail, "line 28\nline 29\nline 30")
        XCTAssertEqual(ServerLauncher.logTail(at: root.appendingPathComponent("missing.log")), "")
    }
}
