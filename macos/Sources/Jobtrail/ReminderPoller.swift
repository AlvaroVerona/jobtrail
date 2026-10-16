import AppKit
import Foundation
import Observation
import UserNotifications

/// Checks the server for due reminders every few minutes, shows how many in the Dock, and
/// announces each one once a day. It only reads: nothing the app does here changes any data.
@MainActor
@Observable
final class ReminderPoller {
    private(set) var snapshot = ReminderSnapshot(due: 0, items: [])

    @ObservationIgnored private let fetch: @Sendable (URL) async throws -> ReminderSnapshot
    @ObservationIgnored private let notifier: ReminderNotifier
    @ObservationIgnored private let setBadge: @MainActor (String?) -> Void
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private let today: @MainActor () -> String
    @ObservationIgnored private let interval: Duration
    @ObservationIgnored private var baseURL: URL?
    @ObservationIgnored private var loop: Task<Void, Never>?

    static let notifiedKey = "remindersNotified"

    init(
        fetch: @escaping @Sendable (URL) async throws -> ReminderSnapshot = ReminderPoller.fetchFromServer,
        notifier: ReminderNotifier,
        setBadge: @escaping @MainActor (String?) -> Void = { NSApp?.dockTile.badgeLabel = $0 },
        defaults: UserDefaults = .standard,
        today: @escaping @MainActor () -> String = { ReminderPolicy.dayString(Date()) },
        interval: Duration = .seconds(300)
    ) {
        self.fetch = fetch
        self.notifier = notifier
        self.setBadge = setBadge
        self.defaults = defaults
        self.today = today
        self.interval = interval
    }

    func start(baseURL: URL) {
        stop()
        self.baseURL = baseURL
        loop = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            while !Task.isCancelled {
                await self?.pollOnce()
                guard let interval = self?.interval else { return }
                try? await Task.sleep(for: interval)
            }
        }
    }

    func stop() {
        stopLoopOnly()
        setBadge(nil)
    }

    /// Stops the timer but leaves the Dock badge as it is. (Tests drive `pollOnce` by hand.)
    func stopLoopOnly() {
        loop?.cancel()
        loop = nil
    }

    /// One check. A failed request (server restarting, say) leaves everything as it was.
    func pollOnce() async {
        guard let baseURL, let result = try? await fetch(baseURL) else { return }
        snapshot = result
        setBadge(ReminderPolicy.badge(forDue: result.due))

        let day = today()
        let stored = (defaults.dictionary(forKey: Self.notifiedKey) as? [String: String]) ?? [:]
        var notified = Dictionary(uniqueKeysWithValues: stored.compactMap { key, value in Int(key).map { ($0, value) } })
        for item in ReminderPolicy.itemsToNotify(result.items, notified: notified, today: day) {
            notifier.announce(item)
            notified[item.id] = day
        }
        let kept = ReminderPolicy.pruned(notified, keeping: day)
        defaults.set(Dictionary(uniqueKeysWithValues: kept.map { (String($0.key), $0.value) }), forKey: Self.notifiedKey)
    }

    nonisolated static func fetchFromServer(_ base: URL) async throws -> ReminderSnapshot {
        var request = URLRequest(url: base.appendingPathComponent("api/reminders"))
        request.timeoutInterval = 3
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(ReminderSnapshot.self, from: data)
    }
}

/// Real notifications. They only work from the built `.app`; run from the command line or in tests
/// (where there is no app bundle) this does nothing instead of crashing.
@MainActor
final class SystemNotifier: NSObject, ReminderNotifier, UNUserNotificationCenterDelegate {
    private let open: @MainActor (String) -> Void
    private var asked = false

    init(open: @escaping @MainActor (String) -> Void) {
        self.open = open
        super.init()
    }

    private var isRealApp: Bool { Bundle.main.bundleURL.pathExtension == "app" }

    func announce(_ item: ReminderItem) {
        guard isRealApp else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        if !asked {
            asked = true
            center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
        let content = UNMutableNotificationContent()
        content.title = "jobtrail"
        content.body = item.title
        content.sound = .default
        content.userInfo = ["url": item.url]
        center.add(UNNotificationRequest(identifier: "reminder-\(item.id)", content: content, trigger: nil))
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        guard let path = response.notification.request.content.userInfo["url"] as? String else { return }
        await MainActor.run {
            NSApp.activate(ignoringOtherApps: true)
            open(path)
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}
