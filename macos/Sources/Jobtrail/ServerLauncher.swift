import Darwin
import Foundation
import Observation

enum ServerState: Equatable {
    case idle
    case starting
    case running(URL)
    case failed(String)
}

/// Starts `jobtrail serve` as a child process and stops it again.
///
/// Lessons carried over from the BusinessConsultant app: shutdown has to be synchronous (a Task
/// started while quitting may never run), and a GUI app does not inherit the shell's PATH, which is
/// why the server is started from the project's own virtualenv rather than through `uv`.
@MainActor
@Observable
final class ServerLauncher {
    private(set) var state: ServerState = .idle
    private(set) var config: ServerConfig?
    var processIdentifier: Int32? { process?.processIdentifier }

    private let configProvider: () throws -> ServerConfig
    private let healthCheck: @Sendable (URL) async -> Bool
    private var process: Process?
    private var logHandle: FileHandle?
    private var stoppingOnPurpose = false

    init(
        configProvider: @escaping () throws -> ServerConfig,
        healthCheck: @escaping @Sendable (URL) async -> Bool = ServerLauncher.defaultHealthCheck
    ) {
        self.configProvider = configProvider
        self.healthCheck = healthCheck
    }

    enum LaunchError: Error, LocalizedError {
        case exitedEarly(Int32)
        case timedOut(TimeInterval)

        var errorDescription: String? {
            switch self {
            case .exitedEarly(let code):
                return "The jobtrail server stopped right after starting (exit code \(code))."
            case .timedOut(let seconds):
                return "The jobtrail server did not answer within \(Int(seconds)) seconds."
            }
        }
    }

    /// Resolves the configuration again each time, so fixing the problem (`uv sync`) and pressing
    /// Retry works without restarting the app.
    func start() async {
        if state == .starting { return }
        if case .running = state { return }
        state = .starting

        let config: ServerConfig
        do {
            config = try configProvider()
        } catch {
            state = .failed(error.localizedDescription)
            return
        }
        self.config = config

        do {
            let port = try FreePort.find()
            try launch(config: config, port: port)
            let url = URL(string: "http://127.0.0.1:\(port)")!
            try await waitUntilHealthy(url: url, timeout: config.startupTimeout)
            state = .running(url)
        } catch {
            stopProcess()
            let tail = Self.logTail(at: config.logURL)
            let detail = tail.isEmpty ? "" : "\n\nLast lines of the server log:\n\(tail)"
            state = .failed(error.localizedDescription + detail)
        }
    }

    func restart() async {
        shutdown()
        state = .idle
        await start()
    }

    /// Synchronous on purpose: this runs while the app is quitting.
    func shutdown() {
        stopProcess()
    }

    // MARK: - Process handling

    private func launch(config: ServerConfig, port: Int) throws {
        try FileManager.default.createDirectory(
            at: config.logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: config.logURL.path, contents: nil)
        let handle = try FileHandle(forWritingTo: config.logURL)

        let process = Process()
        process.executableURL = config.executableURL
        process.arguments = config.arguments
        process.currentDirectoryURL = config.repoURL
        process.environment = config.makeEnvironment(
            port: port,
            parentPID: ProcessInfo.processInfo.processIdentifier,
            base: ProcessInfo.processInfo.environment
        )
        process.standardOutput = handle
        process.standardError = handle
        process.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            Task { @MainActor in self?.processEnded(code: code) }
        }

        stoppingOnPurpose = false
        try process.run()
        self.process = process
        self.logHandle = handle
    }

    private func processEnded(code: Int32) {
        guard !stoppingOnPurpose, case .running = state else { return }
        state = .failed("The jobtrail server stopped unexpectedly (exit code \(code)).")
    }

    private func stopProcess() {
        stoppingOnPurpose = true
        if let process, process.isRunning {
            process.terminate()  // SIGTERM: the server shuts down cleanly
            let deadline = Date().addingTimeInterval(1.0)
            while process.isRunning, Date() < deadline {
                usleep(50_000)
            }
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
            }
        }
        process = nil
        try? logHandle?.close()
        logHandle = nil
    }

    private func waitUntilHealthy(url: URL, timeout: TimeInterval) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let process, !process.isRunning {
                throw LaunchError.exitedEarly(process.terminationStatus)
            }
            if await healthCheck(url) { return }
            try await Task.sleep(for: .milliseconds(250))
        }
        throw LaunchError.timedOut(timeout)
    }

    // MARK: - Helpers

    nonisolated static func defaultHealthCheck(_ base: URL) async -> Bool {
        var request = URLRequest(url: base.appendingPathComponent("health"))
        request.timeoutInterval = 1
        guard let (data, response) = try? await URLSession.shared.data(for: request),
            (response as? HTTPURLResponse)?.statusCode == 200,
            let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return body["status"] as? String == "ok"
    }

    /// The last few lines of the server log, for an error message.
    nonisolated static func logTail(at url: URL, lines: Int = 12) -> String {
        guard let data = try? Data(contentsOf: url), !data.isEmpty else { return "" }
        let text = String(decoding: data.suffix(6000), as: UTF8.self)
        return text.split(separator: "\n", omittingEmptySubsequences: true)
            .suffix(lines).joined(separator: "\n")
    }
}
