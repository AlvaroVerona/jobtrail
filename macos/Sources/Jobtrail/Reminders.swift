import Foundation

/// What the server's `/api/reminders` returns: what is due today or overdue.
struct ReminderItem: Codable, Equatable, Sendable {
    let id: Int
    let title: String
    let dueOn: String
    let url: String

    enum CodingKeys: String, CodingKey {
        case id, title, url
        case dueOn = "due_on"
    }
}

struct ReminderSnapshot: Codable, Equatable, Sendable {
    let due: Int
    let items: [ReminderItem]
}

/// The decisions, kept apart from any UI so they can be tested.
enum ReminderPolicy {
    /// A reminder is announced once per day while it stays due: not on every check, but not
    /// forgotten after one banner either.
    static func itemsToNotify(
        _ items: [ReminderItem], notified: [Int: String], today: String
    ) -> [ReminderItem] {
        items.filter { notified[$0.id] != today }
    }

    static func badge(forDue count: Int) -> String? {
        count > 0 ? String(count) : nil
    }

    /// `yyyy-MM-dd` in the user's own calendar and time zone.
    static func dayString(_ date: Date, calendar: Calendar = .current) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// Forget days that have passed so the stored map does not grow forever.
    static func pruned(_ notified: [Int: String], keeping today: String) -> [Int: String] {
        notified.filter { $0.value == today }
    }
}

/// Where announcements go. The real one uses the system's notifications.
@MainActor
protocol ReminderNotifier {
    func announce(_ item: ReminderItem)
}
