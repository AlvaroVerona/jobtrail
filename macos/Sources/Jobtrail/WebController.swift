import AppKit
import Observation
import WebKit

/// The handle the menus use to drive the web view that SwiftUI owns.
@MainActor
@Observable
final class WebController {
    @ObservationIgnored private weak var webView: WKWebView?
    private(set) var baseURL: URL?

    func attach(_ webView: WKWebView, baseURL: URL) {
        self.webView = webView
        self.baseURL = baseURL
    }

    func reload() { webView?.reload() }
    func goBack() { webView?.goBack() }
    func goForward() { webView?.goForward() }

    func zoomIn() { adjustZoom(by: 0.1) }
    func zoomOut() { adjustZoom(by: -0.1) }
    func resetZoom() { webView?.pageZoom = 1.0 }

    private func adjustZoom(by delta: CGFloat) {
        guard let webView else { return }
        webView.pageZoom = min(2.0, max(0.5, webView.pageZoom + delta))
    }

    /// Opens one of jobtrail's own pages, e.g. "/board".
    func open(path: String) {
        guard let baseURL, let url = URL(string: path, relativeTo: baseURL) else { return }
        webView?.load(URLRequest(url: url))
    }

    func openInBrowser() {
        let target = webView?.url ?? baseURL
        if let target { NSWorkspace.shared.open(target) }
    }
}
