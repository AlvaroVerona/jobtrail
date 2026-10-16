import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Closing the window quits the app, and with it the server (this is a single-window tool).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        // Synchronous on purpose, as in the BusinessConsultant app: a Task started here may not
        // run before the process exits, which left the server behind.
        AppModel.shared.reminders.stop()
        AppModel.shared.launcher.shutdown()
    }
}

@main
struct JobtrailApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        Window("jobtrail", id: "main") {
            RootView(model: AppModel.shared)
        }
        .defaultSize(width: 1320, height: 880)
        .commands { JobtrailCommands(model: AppModel.shared) }
    }
}

struct JobtrailCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .newItem) {}

        CommandGroup(after: .toolbar) {
            Button("Reload") { model.controller.reload() }
                .keyboardShortcut("r")
            Button("Back") { model.controller.goBack() }
                .keyboardShortcut("[")
            Button("Forward") { model.controller.goForward() }
                .keyboardShortcut("]")
            Divider()
            Button("Zoom In") { model.controller.zoomIn() }
                .keyboardShortcut("+")
            Button("Zoom Out") { model.controller.zoomOut() }
                .keyboardShortcut("-")
            Button("Actual Size") { model.controller.resetZoom() }
                .keyboardShortcut("0")
        }

        CommandMenu("Go") {
            ForEach(Array(Self.pages.enumerated()), id: \.offset) { index, page in
                Button(page.title) { model.controller.open(path: page.path) }
                    .keyboardShortcut(KeyEquivalent(Character(String(index + 1))))
            }
        }

        CommandMenu("Server") {
            Button("Open in Browser") { model.controller.openInBrowser() }
                .keyboardShortcut("o", modifiers: [.command, .shift])
            Button("Open Data Folder") { model.showDataFolder() }
            Button("Show Server Log") { model.showServerLog() }
            Button("Check Reminders Now") { Task { await model.reminders.pollOnce() } }
            Divider()
            Button("Restart Server") { Task { await model.launcher.restart() } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
        }
    }

    static let pages: [(title: String, path: String)] = [
        ("Board", "/board"),
        ("Applications", "/applications"),
        ("Postings", "/postings"),
        ("CV", "/cv"),
        ("Follow-ups", "/followups"),
        ("Analytics", "/analytics"),
        ("Settings", "/settings"),
    ]
}
