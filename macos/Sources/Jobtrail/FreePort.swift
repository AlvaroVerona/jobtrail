import Darwin
import Foundation

enum FreePort {
    struct Failure: Error, LocalizedError {
        let step: String
        var errorDescription: String? { "Could not find a free local port (\(step))." }
    }

    /// Asks the system for an unused loopback port. A fresh port on every launch means the app
    /// never fights a development server for 8000.
    static func find() throws -> Int {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw Failure(step: "socket") }
        defer { close(descriptor) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr.s_addr = inet_addr("127.0.0.1")

        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { throw Failure(step: "bind") }

        var assigned = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &assigned) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(descriptor, $0, &length)
            }
        }
        guard named == 0 else { throw Failure(step: "getsockname") }
        return Int(UInt16(bigEndian: assigned.sin_port))
    }
}
