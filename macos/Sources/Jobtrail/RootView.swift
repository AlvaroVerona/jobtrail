import SwiftUI

struct RootView: View {
    let model: AppModel

    var body: some View {
        Group {
            switch model.launcher.state {
            case .idle, .starting:
                StartingView()
            case .running(let url):
                JobtrailWebView(url: url, controller: model.controller)
                    .id(url)  // a restarted server has a new port, so build a new web view
            case .failed(let message):
                FailedView(message: message, model: model)
            }
        }
        .frame(minWidth: 900, minHeight: 620)
        .task { await model.launcher.start() }
        .onChange(of: model.launcher.state) { _, state in
            if case .running(let url) = state {
                model.reminders.start(baseURL: url)
            } else {
                model.reminders.stop()
            }
        }
    }
}

private struct StartingView: View {
    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text("Starting jobtrail…")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct FailedView: View {
    let message: String
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("jobtrail could not start", systemImage: "exclamationmark.triangle.fill")
                .font(.title2.bold())
                .foregroundStyle(.orange)
            ScrollView {
                Text(message)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(10)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            HStack {
                Button("Try again") { Task { await model.launcher.restart() } }
                    .keyboardShortcut(.defaultAction)
                Button("Show server log") { model.showServerLog() }
            }
        }
        .padding(28)
        .frame(maxWidth: 720, maxHeight: .infinity, alignment: .topLeading)
        .frame(maxWidth: .infinity)
    }
}
