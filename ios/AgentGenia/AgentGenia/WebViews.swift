import SafariServices
import SwiftUI
import WebKit

struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let controller = SFSafariViewController(url: url)
        controller.preferredControlTintColor = UIColor(Color.accentColor)
        controller.dismissButtonStyle = .close
        return controller
    }

    func updateUIViewController(_ uiViewController: SFSafariViewController, context: Context) {}
}

struct ComputerViewer: UIViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator(origin: Self.origin(of: url)) }

    private static func origin(of url: URL) -> String {
        let scheme = url.scheme == "wss" ? "https" : (url.scheme ?? "")
        let defaultPort = scheme == "https" ? 443 : nil
        return "\(scheme)://\(url.host ?? ""):\(url.port ?? defaultPort ?? -1)"
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = false
        view.scrollView.keyboardDismissMode = .interactive
        var request = URLRequest(url: url)
        request.setValue("true", forHTTPHeaderField: "X-Daytona-Skip-Preview-Warning")
        view.load(request)
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        guard uiView.url == nil else { return }
        uiView.load(URLRequest(url: url))
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        let origin: String
        init(origin: String) { self.origin = origin }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url,
                  url.scheme == "https" || url.scheme == "wss"
            else { decisionHandler(.cancel); return }
            let nextOrigin = ComputerViewer.origin(of: url)
            decisionHandler(nextOrigin == origin ? .allow : .cancel)
        }
    }
}
