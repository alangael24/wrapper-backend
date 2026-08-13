import AuthenticationServices
import CryptoKit
import Security
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Group {
            switch model.phase {
            case .loading:
                ProgressView("Abriendo Agent Genia…")
            case .signedOut:
                LoginView()
            case .ready:
                MainView()
            }
        }
        .tint(.accentColor)
        .sheet(item: $model.browserRequest, onDismiss: { model.dismissBrowser() }) { request in
            if request.purpose == .computer {
                NavigationStack {
                    ComputerViewer(url: request.url)
                        .ignoresSafeArea(edges: .bottom)
                        .navigationTitle("Computadora")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .topBarTrailing) {
                                Button("Cerrar") { model.dismissBrowser() }
                            }
                        }
                }
            } else {
                SafariView(url: request.url)
                    .ignoresSafeArea()
            }
        }
        .alert(
            "Agent Genia",
            isPresented: Binding(
                get: { model.alertMessage != nil },
                set: { if !$0 { model.alertMessage = nil } }
            )
        ) {
            Button("OK") { model.alertMessage = nil }
        } message: {
            Text(model.alertMessage ?? "")
        }
    }
}

private struct LoginView: View {
    @Environment(AppModel.self) private var model
    @State private var appleNonce = ""

    var body: some View {
        VStack(spacing: 26) {
            Spacer()
            MascotView(color: "#2f91f5", shape: .bean)
                .frame(width: 116, height: 116)
                .shadow(color: Color.blue.opacity(0.18), radius: 28, y: 12)
            VStack(spacing: 8) {
                Text("Agent Genia")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                Text("Dilo una vez. Déjalo hecho.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
            Button {
                Task { await model.beginSignIn() }
            } label: {
                HStack {
                    Image(systemName: "person.crop.circle.badge.checkmark")
                    Text("Continuar con Google")
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 5)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(model.isBusy)
            .overlay { if model.isBusy { ProgressView().tint(.white) } }
            SignInWithAppleButton(.signIn) { request in
                let nonce = secureNonce()
                appleNonce = nonce
                request.requestedScopes = [.fullName, .email]
                request.nonce = SHA256.hash(data: Data(nonce.utf8))
                    .map { String(format: "%02x", $0) }
                    .joined()
            } onCompletion: { result in
                switch result {
                case .success(let authorization):
                    guard
                        let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                        let identityData = credential.identityToken,
                        let codeData = credential.authorizationCode,
                        let identityToken = String(data: identityData, encoding: .utf8),
                        let authorizationCode = String(data: codeData, encoding: .utf8),
                        !appleNonce.isEmpty
                    else {
                        model.alertMessage = "Apple devolvió una autorización incompleta."
                        return
                    }
                    let name = credential.fullName.flatMap {
                        PersonNameComponentsFormatter().string(from: $0).nilIfBlank
                    }
                    Task {
                        await model.completeAppleSignIn(
                            identityToken: identityToken,
                            authorizationCode: authorizationCode,
                            nonce: appleNonce,
                            name: name
                        )
                        appleNonce = ""
                    }
                case .failure(let error):
                    appleNonce = ""
                    if (error as? ASAuthorizationError)?.code != .canceled {
                        model.alertMessage = "No fue posible iniciar sesión con Apple."
                    }
                }
            }
            .signInWithAppleButtonStyle(.black)
            .frame(height: 50)
            .disabled(model.isBusy)
            Text("Tus bots, conectores y computadora se mantienen separados por cuenta.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Spacer()
        }
        .padding(32)
        .frame(maxWidth: 520)
        .frame(maxWidth: .infinity)
        .background(Color(uiColor: .systemGroupedBackground))
    }
}

private func secureNonce(length: Int = 32) -> String {
    let alphabet = Array("0123456789ABCDEFGHIJKLMNOPQRSTUVXYZabcdefghijklmnopqrstuvwxyz-._")
    var bytes = [UInt8](repeating: 0, count: length)
    if SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess {
        return String(bytes.map { alphabet[Int($0) % alphabet.count] })
    }
    return UUID().uuidString.replacingOccurrences(of: "-", with: "")
}

private extension String {
    var nilIfBlank: String? { trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : self }
}

private struct MainView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        NavigationSplitView {
            List(selection: $model.destination) {
                Section {
                    Label("Agent Genia", systemImage: "sparkles")
                        .font(.headline)
                        .foregroundStyle(.primary)
                }
                Section("Bots") {
                    ForEach(model.bots) { bot in
                        NavigationLink(value: Destination.bot(bot.id)) {
                            BotSidebarRow(bot: bot, running: model.runningBotIDs.contains(bot.id))
                        }
                    }
                    Button {
                        Task { await model.createBot() }
                    } label: {
                        Label("Nuevo bot", systemImage: "plus")
                    }
                }
                Section {
                    NavigationLink(value: Destination.plugins) {
                        Label("Plugins", systemImage: "puzzlepiece.extension")
                    }
                    NavigationLink(value: Destination.account) {
                        Label("Cuenta", systemImage: "person.crop.circle")
                    }
                }
            }
            .listStyle(.sidebar)
            .navigationTitle("Agent Genia")
        } detail: {
            switch model.destination {
            case .bot(let id):
                BotView(botID: id)
            case .plugins:
                PluginsView()
            case .account:
                AccountView()
            case nil:
                ContentUnavailableView(
                    "Crea tu primer bot",
                    systemImage: "plus.circle",
                    description: Text("Toca + Nuevo bot para comenzar.")
                )
            }
        }
    }
}

private struct BotSidebarRow: View {
    let bot: BotProfile
    let running: Bool

    var body: some View {
        HStack(spacing: 12) {
            MascotView(color: bot.color, shape: bot.shape)
                .frame(width: 38, height: 38)
            VStack(alignment: .leading, spacing: 2) {
                Text(bot.name).fontWeight(.semibold).lineLimit(1)
                Text(bot.messages.last?.text ?? "Listo para comenzar")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            if running { ProgressView().controlSize(.small) }
        }
        .padding(.vertical, 3)
    }
}

private struct BotView: View {
    @Environment(AppModel.self) private var model
    let botID: UUID
    @State private var message = ""
    @State private var showingSettings = false
    @State private var showingComputer = false

    private var bot: BotProfile? { model.bots.first(where: { $0.id == botID }) }

    var body: some View {
        Group {
            if let bot {
                VStack(spacing: 0) {
                    ChatTimeline(bot: bot)
                    Composer(text: $message, isRunning: model.runningBotIDs.contains(botID)) {
                        let outgoing = message
                        message = ""
                        Task { await model.sendMessage(botID: botID, text: outgoing) }
                    }
                }
                .navigationTitle(bot.name)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItemGroup(placement: .topBarTrailing) {
                        Button {
                            showingComputer = true
                        } label: {
                            Image(systemName: "desktopcomputer")
                        }
                        .accessibilityLabel("Computadora")
                        Button { showingSettings = true } label: { Image(systemName: "slider.horizontal.3") }
                            .accessibilityLabel("Personalizar bot")
                    }
                }
                .sheet(isPresented: $showingSettings) { BotSettingsView(botID: botID) }
                .sheet(isPresented: $showingComputer) { ComputerPanel(botID: botID) }
                .task(id: botID) { await model.sendInitialMessageIfNeeded(botID: botID) }
            } else {
                ContentUnavailableView("Bot no encontrado", systemImage: "exclamationmark.circle")
            }
        }
    }
}

private struct ChatTimeline: View {
    @Environment(AppModel.self) private var model
    let bot: BotProfile

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if bot.messages.isEmpty && model.runningBotIDs.contains(bot.id) {
                        HStack(spacing: 10) { ProgressView(); Text("\(bot.name) está preparando la conversación…") }
                            .foregroundStyle(.secondary)
                            .padding()
                    }
                    ForEach(bot.messages) { item in
                        MessageBubble(message: item, botID: bot.id)
                            .id(item.id)
                    }
                }
                .padding()
            }
            .onChange(of: bot.messages.count) {
                if let last = bot.messages.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
        .background(Color(uiColor: .systemBackground))
    }
}

private struct MessageBubble: View {
    @Environment(AppModel.self) private var model
    let message: BotMessage
    let botID: UUID

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 46) }
            VStack(alignment: .leading, spacing: 10) {
                Text(message.text)
                    .textSelection(.enabled)
                if message.role == .assistant, let widget = message.widget {
                    QuestionWidgetView(widget: widget) { value in
                        Task { await model.sendMessage(botID: botID, text: value) }
                    }
                }
            }
            .padding(14)
            .background(message.role == .user ? Color.accentColor : Color(uiColor: .secondarySystemBackground))
            .foregroundStyle(message.role == .user ? Color.white : Color.primary)
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            if message.role == .assistant { Spacer(minLength: 28) }
        }
    }
}

private struct QuestionWidgetView: View {
    let widget: BotQuestionWidget
    let submit: (String) -> Void
    @State private var custom = ""
    @State private var answered = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(widget.prompt).font(.headline)
            if !widget.helpText.isEmpty { Text(widget.helpText).font(.subheadline).foregroundStyle(.secondary) }
            ForEach(widget.options) { option in
                Button {
                    answered = true
                    submit(option.value)
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(option.label).foregroundStyle(.primary)
                            if !option.description.isEmpty {
                                Text(option.description).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
                    }
                    .padding(10)
                    .background(Color(uiColor: .systemBackground), in: RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)
                .disabled(answered)
            }
            if widget.allowCustom {
                HStack {
                    TextField("Escribe tu respuesta", text: $custom)
                        .textFieldStyle(.roundedBorder)
                    Button("Enviar") {
                        let value = custom
                        custom = ""
                        answered = true
                        submit(value)
                    }
                    .disabled(custom.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || answered)
                }
            }
        }
    }
}

private struct Composer: View {
    @Binding var text: String
    let isRunning: Bool
    let submit: () -> Void

    var body: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Mensaje", text: $text, axis: .vertical)
                .lineLimit(1...5)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 20))
                .onSubmit(submit)
            Button(action: submit) {
                Image(systemName: isRunning ? "hourglass" : "arrow.up")
                    .fontWeight(.bold)
                    .frame(width: 42, height: 42)
                    .background(Color.accentColor, in: Circle())
                    .foregroundStyle(.white)
            }
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isRunning)
        }
        .padding()
        .background(.bar)
    }
}
