import AppKit
import SwiftUI
import WebKit

/// The window's content: jobtrail's own web UI in a WKWebView.
struct JobtrailWebView: NSViewRepresentable {
    let url: URL
    let controller: WebController

    func makeCoordinator() -> Coordinator {
        Coordinator(policy: NavigationPolicy(serverPort: url.port ?? 0))
    }

    func makeNSView(context: Context) -> WKWebView {
        let view = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        controller.attach(view, baseURL: url)
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let policy: NavigationPolicy

        init(policy: NavigationPolicy) {
            self.policy = policy
        }

        // MARK: Navigation: stay inside jobtrail, send everything else to the default browser.

        func webView(
            _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction
        ) async -> WKNavigationActionPolicy {
            switch policy.decide(for: navigationAction.request.url) {
            case .allow:
                return .allow
            case .openExternally:
                if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
                return .cancel
            case .block:
                return .cancel
            }
        }

        /// Links with target="_blank" (the posting's original page) land here.
        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if let url = navigationAction.request.url, policy.decide(for: url) == .openExternally {
                NSWorkspace.shared.open(url)
            }
            return nil
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            webView.reload()
        }

        // MARK: Dialogs. WKWebView shows nothing by itself: without these, confirm() returns
        // false and every "Delete" button in the page would silently do nothing.

        func webView(
            _ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo
        ) async {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }

        func webView(
            _ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
            initiatedByFrame frame: WKFrameInfo
        ) async -> Bool {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            return alert.runModal() == .alertFirstButtonReturn
        }

        func webView(
            _ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
            defaultText: String?, initiatedByFrame frame: WKFrameInfo
        ) async -> String? {
            let alert = NSAlert()
            alert.messageText = prompt
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
            field.stringValue = defaultText ?? ""
            alert.accessoryView = field
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            return alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil
        }

        /// The CV upload on the CV page.
        func webView(
            _ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
            initiatedByFrame frame: WKFrameInfo
        ) async -> [URL]? {
            let panel = NSOpenPanel()
            panel.canChooseFiles = true
            panel.canChooseDirectories = parameters.allowsDirectories
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            return panel.runModal() == .OK ? panel.urls : nil
        }
    }
}
