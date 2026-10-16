import AppKit
import Foundation
import Observation

/// The app's single server and web controller, shared by the window and the menus.
@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    let controller = WebController()
    let launcher: ServerLauncher

    private init() {
        launcher = ServerLauncher(configProvider: {
            try ServerConfig.resolve(
                infoDictionary: Bundle.main.infoDictionary,
                environment: ProcessInfo.processInfo.environment,
                defaults: .standard
            )
        })
    }

    func showServerLog() {
        let url = launcher.config?.logURL
            ?? ServerConfig.defaultLogsDirectory.appendingPathComponent("server.log")
        NSWorkspace.shared.open(url)
    }

    func showDataFolder() {
        guard let folder = launcher.config?.effectiveDataDirectory else { return }
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }
}
