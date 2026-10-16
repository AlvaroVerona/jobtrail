import Darwin
import XCTest

@testable import Jobtrail

final class FreePortTests: XCTestCase {
    func testReturnsAnUnprivilegedPort() throws {
        let port = try FreePort.find()
        XCTAssertGreaterThan(port, 1024)
        XCTAssertLessThanOrEqual(port, 65535)
    }

    func testThePortCanActuallyBeBound() throws {
        let port = try FreePort.find()
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(descriptor) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(port).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        XCTAssertEqual(result, 0)
    }
}

final class NavigationPolicyTests: XCTestCase {
    private let policy = NavigationPolicy(serverPort: 5123)

    private func decide(_ string: String?) -> NavigationDecision {
        policy.decide(for: string.flatMap(URL.init(string:)))
    }

    func testJobtrailsOwnPagesStayInTheWindow() {
        XCTAssertEqual(decide("http://127.0.0.1:5123/board"), .allow)
        XCTAssertEqual(decide("http://127.0.0.1:5123/applications/3#outreach"), .allow)
    }

    func testAnyOtherWebAddressOpensInTheBrowser() {
        XCTAssertEqual(decide("https://example.com/job/1"), .openExternally)
        XCTAssertEqual(decide("http://127.0.0.1:9999/board"), .openExternally)  // another local server
        XCTAssertEqual(decide("http://localhost:5123/board"), .openExternally)  // not our exact origin
        XCTAssertEqual(decide("https://127.0.0.1:5123/board"), .allow)  // same host and port
    }

    func testLocalFilesAndScriptsNeverLoad() {
        XCTAssertEqual(decide("file:///etc/passwd"), .block)
        XCTAssertEqual(decide("javascript:alert(1)"), .block)
    }

    func testOtherSchemesGoToTheirApps() {
        XCTAssertEqual(decide("mailto:someone@example.com"), .openExternally)
        XCTAssertEqual(decide("tel:123"), .openExternally)
    }

    func testBlankAndInternalPagesAreAllowed() {
        XCTAssertEqual(decide("about:blank"), .allow)
        XCTAssertEqual(decide("data:text/html,hi"), .allow)
        XCTAssertEqual(decide(nil), .allow)
    }
}
