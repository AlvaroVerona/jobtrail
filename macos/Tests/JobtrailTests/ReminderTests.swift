import XCTest

@testable import Jobtrail

final class ReminderPolicyTests: XCTestCase {
    private func item(_ id: Int) -> ReminderItem {
        ReminderItem(id: id, title: "Follow up \(id)", dueOn: "2026-10-16", url: "/contacts/\(id)")
    }

    func testTheServersJSONDecodes() throws {
        let json = """
            {"due": 2, "items": [
              {"id": 7, "title": "Follow up with Sam (Acme)", "due_on": "2026-10-15", "url": "/contacts/3"},
              {"id": 9, "title": "Follow up on Globex", "due_on": "2026-10-16", "url": "/applications/4"}
            ]}
            """
        let snapshot = try JSONDecoder().decode(ReminderSnapshot.self, from: Data(json.utf8))
        XCTAssertEqual(snapshot.due, 2)
        XCTAssertEqual(snapshot.items.map(\.id), [7, 9])
        XCTAssertEqual(snapshot.items[0].dueOn, "2026-10-15")
        XCTAssertEqual(snapshot.items[1].url, "/applications/4")
    }

    func testAnEmptyAnswerDecodes() throws {
        let snapshot = try JSONDecoder().decode(ReminderSnapshot.self, from: Data(#"{"due": 0, "items": []}"#.utf8))
        XCTAssertEqual(snapshot, ReminderSnapshot(due: 0, items: []))
    }

    func testEachReminderIsAnnouncedOncePerDay() {
        let items = [item(1), item(2), item(3)]
        let notified = [1: "2026-10-16", 2: "2026-10-15"]  // 1 today, 2 yesterday
        let toAnnounce = ReminderPolicy.itemsToNotify(items, notified: notified, today: "2026-10-16")
        XCTAssertEqual(toAnnounce.map(\.id), [2, 3])  // 2 is announced again on a new day
    }

    func testNothingIsAnnouncedWhenAllWereAlready() {
        let items = [item(1)]
        XCTAssertTrue(ReminderPolicy.itemsToNotify(items, notified: [1: "2026-10-16"], today: "2026-10-16").isEmpty)
    }

    func testTheBadgeIsEmptyWhenNothingIsDue() {
        XCTAssertNil(ReminderPolicy.badge(forDue: 0))
        XCTAssertEqual(ReminderPolicy.badge(forDue: 3), "3")
    }

    func testTheDayIsTheUsersOwnCalendarDay() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Pacific/Auckland")!  // UTC+12/13
        let utcLateEvening = Date(timeIntervalSince1970: 1_792_000_000)  // evening in UTC
        let utcCalendar = { () -> Calendar in
            var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "UTC")!; return c
        }()
        XCTAssertNotEqual(
            ReminderPolicy.dayString(utcLateEvening, calendar: calendar),
            ReminderPolicy.dayString(utcLateEvening, calendar: utcCalendar),
            "the same instant is a different day in a different time zone")
        XCTAssertEqual(ReminderPolicy.dayString(utcLateEvening, calendar: utcCalendar).count, 10)
    }

    func testOldDaysAreForgotten() {
        let kept = ReminderPolicy.pruned([1: "2026-10-16", 2: "2026-10-10"], keeping: "2026-10-16")
        XCTAssertEqual(kept, [1: "2026-10-16"])
    }
}

@MainActor
final class ReminderPollerTests: XCTestCase {
    private final class Recorder: ReminderNotifier {
        var announced: [Int] = []
        func announce(_ item: ReminderItem) { announced.append(item.id) }
    }

    private var defaults: UserDefaults!
    private var suite: String!

    override func setUp() async throws {
        suite = "jobtrail-reminder-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
    }

    override func tearDown() async throws {
        defaults.removePersistentDomain(forName: suite)
    }

    private nonisolated static func snapshot(_ ids: [Int]) -> ReminderSnapshot {
        ReminderSnapshot(
            due: ids.count,
            items: ids.map { ReminderItem(id: $0, title: "R\($0)", dueOn: "2026-10-16", url: "/contacts/\($0)") })
    }

    private func poller(
        _ recorder: Recorder, badge: BadgeBox, day: DayBox,
        fetch: @escaping @Sendable (URL) async throws -> ReminderSnapshot
    ) -> ReminderPoller {
        let poller = ReminderPoller(
            fetch: fetch, notifier: recorder, setBadge: { badge.value = $0 }, defaults: defaults,
            today: { day.value })
        poller.start(baseURL: URL(string: "http://127.0.0.1:1")!)
        poller.stopLoopOnly()
        return poller
    }

    private final class BadgeBox { var value: String? = "untouched" }
    private final class DayBox { var value = "2026-10-16" }

    func testThePollerShowsTheCountAndAnnouncesEachItemOnce() async {
        let recorder = Recorder(), badge = BadgeBox(), day = DayBox()
        let poller = poller(recorder, badge: badge, day: day) { _ in ReminderPollerTests.snapshot([1, 2]) }

        await poller.pollOnce()
        XCTAssertEqual(badge.value, "2")
        XCTAssertEqual(recorder.announced, [1, 2])

        await poller.pollOnce()  // same day, nothing new
        XCTAssertEqual(recorder.announced, [1, 2])
    }

    func testAStillDueReminderIsAnnouncedAgainTheNextDay() async {
        let recorder = Recorder(), badge = BadgeBox(), day = DayBox()
        let poller = poller(recorder, badge: badge, day: day) { _ in ReminderPollerTests.snapshot([5]) }
        await poller.pollOnce()
        day.value = "2026-10-17"
        await poller.pollOnce()
        XCTAssertEqual(recorder.announced, [5, 5])
    }

    func testNewReminderAppearingLaterIsAnnouncedWithoutRepeatingTheOldOnes() async {
        let recorder = Recorder(), badge = BadgeBox(), day = DayBox()
        let ids = IDsBox([1])
        let poller = poller(recorder, badge: badge, day: day) { _ in ReminderPollerTests.snapshot(ids.value) }
        await poller.pollOnce()
        ids.value = [1, 2]
        await poller.pollOnce()
        XCTAssertEqual(recorder.announced, [1, 2])
        XCTAssertEqual(badge.value, "2")
    }

    func testTheBadgeClearsWhenNothingIsDueAnymore() async {
        let recorder = Recorder(), badge = BadgeBox(), day = DayBox()
        let ids = IDsBox([1])
        let poller = poller(recorder, badge: badge, day: day) { _ in ReminderPollerTests.snapshot(ids.value) }
        await poller.pollOnce()
        XCTAssertEqual(badge.value, "1")
        ids.value = []
        await poller.pollOnce()
        XCTAssertNil(badge.value)
    }

    func testAFailedCheckChangesNothing() async {
        let recorder = Recorder(), badge = BadgeBox(), day = DayBox()
        let ids = IDsBox([1])
        let failing = FlagBox()
        let poller = poller(recorder, badge: badge, day: day) { _ in
            if failing.value { throw URLError(.cannotConnectToHost) }
            return ReminderPollerTests.snapshot(ids.value)
        }
        await poller.pollOnce()
        failing.value = true
        await poller.pollOnce()  // the server is restarting
        XCTAssertEqual(badge.value, "1", "the badge must not flicker off because one check failed")
        XCTAssertEqual(recorder.announced, [1])
    }

    func testAnnouncementsSurviveARelaunch() async {
        let recorder = Recorder(), badge = BadgeBox(), day = DayBox()
        let first = poller(recorder, badge: badge, day: day) { _ in ReminderPollerTests.snapshot([1]) }
        await first.pollOnce()
        let second = poller(recorder, badge: badge, day: day) { _ in ReminderPollerTests.snapshot([1]) }  // app restarted
        await second.pollOnce()
        XCTAssertEqual(recorder.announced, [1], "restarting the app must not repeat today's banner")
    }

    private final class IDsBox: @unchecked Sendable { var value: [Int]; init(_ v: [Int]) { value = v } }
    private final class FlagBox: @unchecked Sendable { var value = false }
}
