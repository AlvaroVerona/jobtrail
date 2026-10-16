import Foundation

/// Everything needed to start the jobtrail server, resolved from the app bundle, the
/// environment and user defaults. Pure logic: nothing here touches the network or runs a
/// process, so it is covered by unit tests.
struct ServerConfig: Equatable, Sendable {
    var repoURL: URL
    var executableURL: URL
    var arguments: [String] = ["serve"]
    /// Where the database lives. `nil` leaves the server's own default (`<repo>/data`), which is
    /// also what `uv run jobtrail serve` uses, so the app and the command line share one database.
    var dataDirectory: URL?
    var logURL: URL
    var startupTimeout: TimeInterval = 30

    var effectiveDataDirectory: URL {
        dataDirectory ?? repoURL.appendingPathComponent("data", isDirectory: true)
    }

    enum Problem: Error, Equatable, LocalizedError {
        case repoNotConfigured
        case repoMissing(String)
        case serverNotInstalled(String)

        var errorDescription: String? {
            switch self {
            case .repoNotConfigured:
                return "This app does not know where the jobtrail project is. Rebuild it with "
                    + "macos/Scripts/build-app.sh, or set the JOBTRAIL_REPO environment variable."
            case .repoMissing(let path):
                return "The jobtrail project was not found at \(path). If you moved it, rebuild "
                    + "the app with macos/Scripts/build-app.sh from its new location."
            case .serverNotInstalled(let path):
                return "jobtrail is not installed in \(path). Run `uv sync` in that folder, "
                    + "then try again."
            }
        }
    }

    /// Key written into the app's Info.plist by build-app.sh.
    static let repoPathKey = "JobtrailRepoPath"
    static let dataDirectoryDefaultsKey = "dataDir"

    static var defaultLogsDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/jobtrail", isDirectory: true)
    }

    static func resolve(
        infoDictionary: [String: Any]?,
        environment: [String: String],
        defaults: UserDefaults,
        logsDirectory: URL = defaultLogsDirectory,
        fileManager: FileManager = .default
    ) throws -> ServerConfig {
        let configured = environment["JOBTRAIL_REPO"].flatMap { $0.isEmpty ? nil : $0 }
            ?? (infoDictionary?[repoPathKey] as? String).flatMap { $0.isEmpty ? nil : $0 }
        guard let repoPath = configured else { throw Problem.repoNotConfigured }

        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: repoPath, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw Problem.repoMissing(repoPath)
        }

        let repo = URL(fileURLWithPath: repoPath, isDirectory: true)
        let executable = repo.appendingPathComponent(".venv/bin/jobtrail")
        guard fileManager.isExecutableFile(atPath: executable.path) else {
            throw Problem.serverNotInstalled(repoPath)
        }

        let dataPath = defaults.string(forKey: dataDirectoryDefaultsKey).flatMap { $0.isEmpty ? nil : $0 }
        return ServerConfig(
            repoURL: repo,
            executableURL: executable,
            dataDirectory: dataPath.map { URL(fileURLWithPath: $0, isDirectory: true) },
            logURL: logsDirectory.appendingPathComponent("server.log")
        )
    }

    /// The environment for the server process. A GUI app starts with almost no environment, so
    /// everything the server needs is stated explicitly.
    func makeEnvironment(port: Int, parentPID: Int32, base: [String: String]) -> [String: String] {
        var environment = base
        environment["JOBTRAIL_HOST"] = "127.0.0.1"
        environment["JOBTRAIL_PORT"] = String(port)
        // The server exits by itself if this app disappears without cleaning up.
        environment["JOBTRAIL_PARENT_PID"] = String(parentPID)
        environment["PYTHONUNBUFFERED"] = "1"
        if let dataDirectory {
            environment["JOBTRAIL_DATA_DIR"] = dataDirectory.path
        }
        return environment
    }
}
