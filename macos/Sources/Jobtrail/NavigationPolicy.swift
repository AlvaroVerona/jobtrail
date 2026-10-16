import Foundation

enum NavigationDecision: Equatable {
    case allow
    case openExternally
    case block
}

/// What the window may show. The window is only for jobtrail's own pages: any other web address
/// (a posting link, a documentation link) opens in the default browser, and local files never load.
struct NavigationPolicy: Sendable {
    let serverPort: Int

    func decide(for url: URL?) -> NavigationDecision {
        guard let url, let scheme = url.scheme?.lowercased() else { return .allow }
        switch scheme {
        case "about", "blob", "data":
            return .allow
        case "http", "https":
            return url.host == "127.0.0.1" && url.port == serverPort ? .allow : .openExternally
        case "file", "javascript":
            return .block
        default:
            return .openExternally  // mailto:, tel:, ...
        }
    }
}
