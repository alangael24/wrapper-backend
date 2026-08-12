import SwiftUI

struct BotSettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let botID: UUID
    @State private var name = ""
    @State private var title = ""
    @State private var description = ""
    @State private var color = "#2f91f5"
    @State private var shape: BotShape = .bean
    @State private var notifications = true

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack {
                        Spacer()
                        MascotView(color: color, shape: shape)
                            .frame(width: 108, height: 108)
                        Spacer()
                    }
                    .listRowBackground(Color.clear)
                }
                Section("Identidad") {
                    TextField("Nombre", text: $name)
                    TextField("Título", text: $title)
                    TextField("Qué hace este agente", text: $description, axis: .vertical)
                        .lineLimit(3...7)
                }
                Section("Forma") {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 14) {
                            ForEach(BotShape.allCases, id: \.self) { candidate in
                                Button { shape = candidate } label: {
                                    MascotView(color: color, shape: candidate)
                                        .frame(width: 52, height: 52)
                                        .padding(5)
                                        .background(shape == candidate ? Color.accentColor.opacity(0.13) : Color.clear)
                                        .clipShape(RoundedRectangle(cornerRadius: 12))
                                        .overlay {
                                            if shape == candidate {
                                                RoundedRectangle(cornerRadius: 12).stroke(Color.accentColor, lineWidth: 2)
                                            }
                                        }
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                }
                Section("Color") {
                    LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 5), spacing: 16) {
                        ForEach(botColors, id: \.self) { candidate in
                            Button { color = candidate } label: {
                                Circle()
                                    .fill(Color(hex: candidate))
                                    .frame(width: 38, height: 38)
                                    .overlay {
                                        if color == candidate { Circle().stroke(.primary, lineWidth: 2).padding(-4) }
                                    }
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, 8)
                }
                Section {
                    Toggle("Notificaciones", isOn: $notifications)
                } footer: {
                    Text("Recibe un aviso cuando el agente termine o necesite tu ayuda.")
                }
            }
            .navigationTitle("Personalizar")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancelar") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Guardar") {
                        Task {
                            await model.updateBot(
                                id: botID,
                                name: name,
                                title: title,
                                description: description,
                                color: color,
                                shape: shape,
                                notificationsEnabled: notifications
                            )
                            dismiss()
                        }
                    }
                }
            }
            .onAppear {
                guard let bot = model.bots.first(where: { $0.id == botID }) else { return }
                name = bot.name
                title = bot.title
                description = bot.description
                color = bot.color
                shape = bot.shape
                notifications = bot.notificationsEnabled
            }
        }
    }
}

struct PluginsView: View {
    @Environment(AppModel.self) private var model

    private var filtered: [ConnectorDefinition] {
        ConnectorDefinition.catalog.filter { connector in
            let matchesSearch = model.searchText.isEmpty
                || connector.name.localizedCaseInsensitiveContains(model.searchText)
                || connector.summary.localizedCaseInsensitiveContains(model.searchText)
                || connector.category.localizedCaseInsensitiveContains(model.searchText)
            let matchesMode = !model.showOnlyConnected || model.connectorStatuses[connector.id]?.connected == true
            return matchesSearch && matchesMode
        }
    }

    private var grouped: [(String, [ConnectorDefinition])] {
        Dictionary(grouping: filtered, by: \.category)
            .sorted { $0.key.localizedStandardCompare($1.key) == .orderedAscending }
    }

    var body: some View {
        @Bindable var model = model
        List {
            Picker("Vista", selection: $model.showOnlyConnected) {
                Text("Marketplace").tag(false)
                Text("Tuyos").tag(true)
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear)
            ForEach(grouped, id: \.0) { category, connectors in
                Section(category) {
                    ForEach(connectors) { connector in
                        ConnectorRow(
                            connector: connector,
                            status: model.connectorStatuses[connector.id]
                        )
                    }
                }
            }
            if filtered.isEmpty {
                ContentUnavailableView.search(text: model.searchText)
                    .listRowBackground(Color.clear)
            }
        }
        .navigationTitle("Plugins")
        .searchable(text: $model.searchText, prompt: "Buscar plugins")
        .refreshable { await model.refreshConnectors() }
        .task { await model.refreshConnectors() }
    }
}

private struct ConnectorRow: View {
    @Environment(AppModel.self) private var model
    let connector: ConnectorDefinition
    let status: ConnectorStatus?

    var body: some View {
        HStack(spacing: 14) {
            ZStack {
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(uiColor: .secondarySystemBackground))
                Image(systemName: connector.symbol)
                    .font(.title2)
                    .foregroundStyle(.primary)
            }
            .frame(width: 48, height: 48)
            VStack(alignment: .leading, spacing: 3) {
                Text(connector.name).fontWeight(.semibold)
                Text(connector.summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                if status?.available == false, let reason = status?.reason, !reason.isEmpty {
                    Text(reason).font(.caption2).foregroundStyle(.orange).lineLimit(1)
                }
            }
            Spacer()
            if status?.connected == true {
                Menu {
                    if let account = status?.account, !account.isEmpty { Text(account) }
                    Button("Desconectar", role: .destructive) {
                        Task { await model.disconnect(connector.id) }
                    }
                } label: {
                    Label("Añadido", systemImage: "checkmark")
                        .font(.subheadline)
                        .foregroundStyle(.green)
                }
            } else {
                Button("Añadir") { Task { await model.connect(connector.id) } }
                    .buttonStyle(.bordered)
                    .disabled(status?.available != true || model.isBusy)
            }
        }
        .padding(.vertical, 5)
    }
}

struct AccountView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        List {
            if let account = model.account {
                Section("Cuenta") {
                    LabeledContent("Nombre", value: account.name.isEmpty ? "—" : account.name)
                    LabeledContent("Email", value: account.email)
                    LabeledContent("Plan", value: model.billing?.tier.capitalized ?? model.profile?.tierLabel ?? "—")
                }
            }
            if let billing = model.billing {
                Section("Suscripción") {
                    if billing.customer {
                        Button("Administrar suscripción") { Task { await model.openBillingPortal() } }
                    } else if billing.configured {
                        ForEach(["basic", "pro"], id: \.self) { tier in
                            if let plan = billing.plans[tier] {
                                Button {
                                    Task { await model.startCheckout(tier: tier) }
                                } label: {
                                    HStack {
                                        VStack(alignment: .leading) {
                                            Text(plan.name).fontWeight(.semibold)
                                            Text("Computadoras persistentes y ejecuciones de agentes")
                                                .font(.caption)
                                                .foregroundStyle(.secondary)
                                        }
                                        Spacer()
                                        Text(plan.amount, format: .currency(code: plan.currency.uppercased()))
                                    }
                                }
                            }
                        }
                        Text("El pago se completa en el sitio seguro de Agent Genia.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("Los pagos no están disponibles en este momento.")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Section {
                Button("Cerrar sesión", role: .destructive) { Task { await model.signOut() } }
            }
            Section("Servicio") {
                LabeledContent("API", value: AppEnvironment.baseURL.host ?? "Agent Genia")
                LabeledContent("Versión", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0")
            }
        }
        .navigationTitle("Cuenta")
        .refreshable { await model.refreshBilling() }
        .task { await model.refreshBilling() }
    }
}

struct ComputerPanel: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let botID: UUID

    private var snapshot: ComputerSnapshot? {
        model.computerBotID == botID ? model.computer : nil
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                ZStack {
                    RoundedRectangle(cornerRadius: 28, style: .continuous)
                        .fill(Color(uiColor: .secondarySystemBackground))
                    Image(systemName: "desktopcomputer")
                        .font(.system(size: 70, weight: .light))
                        .foregroundStyle(.secondary)
                    if snapshot?.state == .running {
                        Circle().fill(.green).frame(width: 16, height: 16)
                            .overlay(Circle().stroke(.white, lineWidth: 3))
                            .offset(x: 54, y: 45)
                    }
                }
                .frame(height: 210)
                VStack(spacing: 8) {
                    Text(stateTitle).font(.title2.bold())
                    Text(stateDescription).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                if model.isBusy || snapshot?.state == .pulling {
                    ProgressView("Preparando computadora…")
                }
                HStack {
                    if snapshot?.state == .running {
                        Button("Abrir") { model.openComputer() }
                            .buttonStyle(.borderedProminent)
                        Button("Hibernar") { Task { await model.handBackComputer(botID: botID) } }
                            .buttonStyle(.bordered)
                    } else if snapshot?.configured != false {
                        Button(snapshot?.state == .hibernated ? "Despertar" : "Crear computadora") {
                            Task { await model.ensureComputer(botID: botID) }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.isBusy)
                    }
                }
                Spacer()
            }
            .padding()
            .navigationTitle("Computadora")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Cerrar") { dismiss() } } }
            .task { await model.loadComputer(botID: botID) }
        }
    }

    private var stateTitle: String {
        return switch snapshot?.state {
        case .running: "Lista para trabajar"
        case .pulling: "Preparando imagen"
        case .hibernated: "En reposo"
        case .error: "Necesita atención"
        case .disabled: "No configurada"
        case .off: "Sin computadora"
        case nil: "Consultando estado"
        }
    }

    private var stateDescription: String {
        if let reason = snapshot?.reason, !reason.isEmpty { return reason }
        return switch snapshot?.state {
        case .running: "Este bot tiene una computadora aislada con archivos y sesiones persistentes."
        case .hibernated: "Tus archivos se conservan; despiértala cuando la necesites."
        case .disabled: "El backend todavía no tiene proveedor de computadoras configurado."
        default: "Cada bot puede trabajar en su propio entorno aislado."
        }
    }
}
