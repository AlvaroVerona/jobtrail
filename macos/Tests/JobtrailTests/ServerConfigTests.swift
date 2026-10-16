import XCTest

@testable import Jobtrail

final class ServerConfigTests: XCTestCase {
    private var root: URL!
    private var defaults: UserDefaults!
    private var suiteName: String!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        suiteName = "jobtrail-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
        defaults.removePersistentDomain(forName: suiteName)
    }

    /// A fake checkout: a folder with an executable `.venv/bin/jobtrail`.
    private func makeRepo(named name: String = "repo", executable: Bool = true) throws -> URL {
        let repo = root.appendingPathComponent(name, isDirectory: true)
        let bin = repo.appendingPathComponent(".venv/bin", isDirectory: true)
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        let tool = bin.appendingPathComponent("jobtrail")
        try "#!/bin/sh\n".write(to: tool, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: executable ? 0o755 : 0o644], ofItemAtPath: tool.path)
        return repo
    }

    private func resolve(info: [String: Any]? = nil, env: [String: String] = [:]) throws -> ServerConfig {
        try ServerConfig.resolve(
            infoDictionary: info, environment: env, defaults: defaults,
            logsDirectory: root.appendingPathComponent("logs"))
    }

    func testReadsTheProjectPathFromTheAppBundle() throws {
        let repo = try makeRepo()
        let config = try resolve(info: [ServerConfig.repoPathKey: repo.path])
        XCTAssertEqual(config.repoURL.path, repo.path)
        XCTAssertEqual(config.executableURL.path, repo.appendingPathComponent(".venv/bin/jobtrail").path)
        XCTAssertEqual(config.arguments, ["serve"])
        XCTAssertEqual(config.logURL.lastPathComponent, "server.log")
    }

    func testTheEnvironmentOverridesTheBundle() throws {
        let bundled = try makeRepo(named: "bundled")
        let other = try makeRepo(named: "other")
        let config = try resolve(
            info: [ServerConfig.repoPathKey: bundled.path], env: ["JOBTRAIL_REPO": other.path])
        XCTAssertEqual(config.repoURL.path, other.path)
    }

    func testNoConfiguredProjectIsAClearProblem() {
        for info in [nil, [ServerConfig.repoPathKey: ""]] as [[String: Any]?] {
            XCTAssertThrowsError(try resolve(info: info)) {
                XCTAssertEqual($0 as? ServerConfig.Problem, .repoNotConfigured)
            }
        }
    }

    func testAMissingOrNonFolderPathIsReported() throws {
        let missing = root.appendingPathComponent("nope").path
        XCTAssertThrowsError(try resolve(info: [ServerConfig.repoPathKey: missing])) {
            XCTAssertEqual($0 as? ServerConfig.Problem, .repoMissing(missing))
        }
        let file = root.appendingPathComponent("a-file")
        try "x".write(to: file, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try resolve(info: [ServerConfig.repoPathKey: file.path])) {
            XCTAssertEqual($0 as? ServerConfig.Problem, .repoMissing(file.path))
        }
    }

    func testAProjectWithoutAnInstalledServerTellsYouToRunUvSync() throws {
        let bare = root.appendingPathComponent("bare", isDirectory: true)
        try FileManager.default.createDirectory(at: bare, withIntermediateDirectories: true)
        XCTAssertThrowsError(try resolve(info: [ServerConfig.repoPathKey: bare.path])) { error in
            XCTAssertEqual(error as? ServerConfig.Problem, .serverNotInstalled(bare.path))
            XCTAssertTrue(error.localizedDescription.contains("uv sync"))
        }
        let notExecutable = try makeRepo(named: "locked", executable: false)
        XCTAssertThrowsError(try resolve(info: [ServerConfig.repoPathKey: notExecutable.path]))
    }

    func testTheDataFolderDefaultsToTheProjectsAndCanBeOverridden() throws {
        let repo = try makeRepo()
        let plain = try resolve(info: [ServerConfig.repoPathKey: repo.path])
        XCTAssertNil(plain.dataDirectory)
        XCTAssertEqual(plain.effectiveDataDirectory.path, repo.appendingPathComponent("data").path)

        defaults.set("/tmp/my-jobtrail-data", forKey: ServerConfig.dataDirectoryDefaultsKey)
        let custom = try resolve(info: [ServerConfig.repoPathKey: repo.path])
        XCTAssertEqual(custom.effectiveDataDirectory.path, "/tmp/my-jobtrail-data")
    }

    func testTheServerEnvironmentIsExplicit() throws {
        let repo = try makeRepo()
        let config = try resolve(info: [ServerConfig.repoPathKey: repo.path])
        let environment = config.makeEnvironment(
            port: 5123, parentPID: 4242, base: ["HOME": "/home/test", "JOBTRAIL_PORT": "8000"])
        XCTAssertEqual(environment["JOBTRAIL_PORT"], "5123")  // ours wins over an inherited value
        XCTAssertEqual(environment["JOBTRAIL_HOST"], "127.0.0.1")
        XCTAssertEqual(environment["JOBTRAIL_PARENT_PID"], "4242")
        XCTAssertEqual(environment["PYTHONUNBUFFERED"], "1")
        XCTAssertEqual(environment["HOME"], "/home/test")
        XCTAssertNil(environment["JOBTRAIL_DATA_DIR"])  // the server's own default applies
    }

    func testACustomDataFolderIsPassedToTheServer() throws {
        let repo = try makeRepo()
        defaults.set("/tmp/my-jobtrail-data", forKey: ServerConfig.dataDirectoryDefaultsKey)
        let config = try resolve(info: [ServerConfig.repoPathKey: repo.path])
        let environment = config.makeEnvironment(port: 1, parentPID: 2, base: [:])
        XCTAssertEqual(environment["JOBTRAIL_DATA_DIR"], "/tmp/my-jobtrail-data")
    }
}
