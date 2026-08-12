import Foundation
import SwiftUI

struct AccountIdentity: Codable, Equatable, Sendable {
    let id: String
    let email: String
    let name: String
    let picture: String
}

struct AccountSession: Codable, Equatable, Sendable {
    var token: String
    var refreshToken: String
    var expiresAt: Int64
    var account: AccountIdentity

    enum CodingKeys: String, CodingKey {
        case token
        case refreshToken = "refresh_token"
        case expiresAt = "expires_at"
        case account
    }
}

struct AccountProfile: Codable, Equatable, Sendable {
    let userID: String
    let name: String?
    let email: String?
    let tier: String
    let tierLabel: String

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case name, email, tier
        case tierLabel = "tier_label"
    }
}

struct BotQuestionOption: Codable, Identifiable, Equatable, Sendable {
    var id: String { value }
    let label: String
    let value: String
    let description: String
}

struct BotQuestionWidget: Codable, Equatable, Sendable {
    let prompt: String
    let helpText: String
    let options: [BotQuestionOption]
    let allowCustom: Bool
    let dismissOnMoveOn: Bool
}

struct BotMessage: Codable, Identifiable, Equatable, Sendable {
    enum Role: String, Codable, Sendable {
        case user
        case assistant
    }

    let id: UUID
    let role: Role
    let text: String
    let widget: BotQuestionWidget?
    let createdAt: Date
}

struct BotProfile: Codable, Identifiable, Equatable, Sendable {
    var id: UUID
    var name: String
    var title: String
    var description: String
    var color: String
    var shape: BotShape
    var notificationsEnabled: Bool
    var connectorIDs: [String]
    var messages: [BotMessage]
    var createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id, name, title, description, color, shape, notificationsEnabled, messages, createdAt
        case connectorIDs = "connectorIds"
    }
}

enum BotShape: String, Codable, CaseIterable, Sendable {
    case circle, bean, square, capsule, triangle, hexagon, cloud, drop
}

struct PersistedAccountState: Codable, Equatable, Sendable {
    var bots: [BotProfile] = []
    var selectedConnectorIDs: [String] = []
    var activeBotID: UUID?
}

struct ConnectorDefinition: Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let symbol: String
    let category: String
    let summary: String

    var logoAsset: String { "logo_" + id.replacingOccurrences(of: "-", with: "_") }

    static let catalog: [ConnectorDefinition] = [
        connector("google-workspace", "Google Workspace", "g.circle.fill", "Trabajo", "Gmail, Drive, Calendar, Contacts y Sheets"),
        connector("slack", "Slack", "number.square.fill", "Trabajo", "Canales, mensajes y coordinación de equipo"),
        connector("notion", "Notion", "doc.text.fill", "Trabajo", "Páginas, bases de datos y conocimiento"),
        connector("salesforce", "Salesforce", "cloud.fill", "Ventas", "Cuentas, contactos y oportunidades"),
        connector("microsoft-365", "Microsoft 365", "square.grid.2x2.fill", "Trabajo", "Outlook, OneDrive, Calendar y Teams"),
        connector("linkedin", "LinkedIn", "person.crop.square.fill", "Ventas", "Contactos y relaciones profesionales"),
        connector("zoom", "Zoom", "video.fill", "Trabajo", "Reuniones y seguimiento de llamadas"),
        connector("github", "GitHub", "chevron.left.forwardslash.chevron.right", "Desarrollo", "Repositorios, issues y pull requests"),
        connector("jira", "Jira", "diamond.fill", "Desarrollo", "Proyectos, tickets y ciclos de trabajo"),
        connector("linear", "Linear", "line.diagonal", "Desarrollo", "Issues, proyectos y ciclos de producto"),
        connector("asana", "Asana", "circle.grid.3x3.fill", "Trabajo", "Proyectos, tareas y responsables"),
        connector("clickup", "ClickUp", "checkmark.circle.fill", "Trabajo", "Tareas, documentos y seguimiento"),
        connector("figma", "Figma", "paintpalette.fill", "Diseño", "Archivos, comentarios y entregables"),
        connector("hubspot", "HubSpot", "point.3.connected.trianglepath.dotted", "Ventas", "Contactos, empresas y oportunidades"),
        connector("canva", "Canva", "wand.and.stars", "Diseño", "Diseños, plantillas y contenido de marca"),
        connector("trello", "Trello", "rectangle.split.2x1.fill", "Trabajo", "Tableros, listas y tarjetas"),
        connector("monday-com", "monday.com", "calendar", "Trabajo", "Tableros, proyectos y automatizaciones"),
        connector("intercom", "Intercom", "message.fill", "Soporte", "Conversaciones y atención al cliente"),
        connector("zendesk", "Zendesk", "ticket.fill", "Soporte", "Tickets y operaciones de soporte"),
        connector("box", "Box", "shippingbox.fill", "Trabajo", "Archivos y colaboración empresarial"),
        connector("dropbox", "Dropbox", "shippingbox.and.arrow.backward.fill", "Trabajo", "Archivos y contenido compartido"),
        connector("docusign", "DocuSign", "signature", "Trabajo", "Firmas y seguimiento de documentos"),
        connector("calendly", "Calendly", "calendar.badge.clock", "Trabajo", "Disponibilidad y reuniones"),
        connector("loom", "Loom", "record.circle", "Trabajo", "Videos, transcripciones y equipo"),
        connector("outreach", "Outreach", "paperplane.fill", "Ventas", "Prospectos, secuencias y actividades"),
        connector("salesloft", "Salesloft", "chart.line.uptrend.xyaxis", "Ventas", "Cadencias y actividades de ventas"),
        connector("apollo", "Apollo", "paperplane.circle.fill", "Ventas", "Personas, empresas y enriquecimiento"),
        connector("clay", "Clay", "tablecells.fill", "Ventas", "Tablas, enriquecimiento y prospección"),
        connector("zoominfo", "ZoomInfo", "person.text.rectangle.fill", "Ventas", "Contactos e inteligencia comercial"),
        connector("nooks", "Nooks", "phone.fill", "Ventas", "Marcador y productividad de ventas"),
        connector("stripe", "Stripe", "creditcard.fill", "Finanzas", "Clientes, pagos, facturas y suscripciones"),
        connector("quickbooks", "QuickBooks", "dollarsign.circle.fill", "Finanzas", "Contabilidad, facturas y gastos"),
        connector("netsuite", "NetSuite", "building.2.fill", "Finanzas", "ERP, clientes y operaciones"),
        connector("ramp", "Ramp", "creditcard.trianglebadge.exclamationmark", "Finanzas", "Tarjetas, gastos y proveedores"),
        connector("workday", "Workday", "person.3.fill", "RR. HH.", "Personas, puestos y recursos humanos"),
        connector("rippling", "Rippling", "person.crop.circle.badge.checkmark", "RR. HH.", "Empleados, nómina y aplicaciones"),
        connector("ashby", "Ashby", "person.crop.rectangle.stack.fill", "RR. HH.", "Candidatos, vacantes y contratación"),
        connector("greenhouse", "Greenhouse", "leaf.fill", "RR. HH.", "Candidatos, entrevistas y vacantes"),
        connector("vercel", "Vercel", "triangle.fill", "Desarrollo", "Proyectos, deployments, dominios y logs"),
        connector("tableau", "Tableau", "chart.bar.xaxis", "Datos", "Fuentes, workbooks y visualizaciones"),
        connector("hex", "Hex", "hexagon.fill", "Datos", "Proyectos, notebooks y análisis"),
        connector("amplitude", "Amplitude", "waveform.path.ecg", "Datos", "Analítica de producto, eventos y cohortes"),
        connector("mixpanel", "Mixpanel", "chart.xyaxis.line", "Datos", "Eventos, funnels y retención"),
        connector("snowflake", "Snowflake", "snowflake", "Datos", "Warehouses, bases y consultas"),
        connector("databricks", "Databricks", "square.3.layers.3d", "Datos", "Lakehouse, notebooks y jobs"),
        connector("mailchimp", "Mailchimp", "envelope.badge.fill", "Marketing", "Audiencias, campañas y automatizaciones"),
        connector("shopify", "Shopify", "bag.fill", "Comercio", "Catálogo, tienda y herramientas"),
        connector("tiendanube", "Tiendanube", "storefront.fill", "Comercio", "Catálogo y contexto de tienda"),
        connector("woocommerce", "WooCommerce", "cart.fill", "Comercio", "Productos y WordPress Commerce")
    ]

    private static func connector(
        _ id: String, _ name: String, _ symbol: String, _ category: String, _ summary: String
    ) -> ConnectorDefinition {
        ConnectorDefinition(id: id, name: name, symbol: symbol, category: category, summary: summary)
    }
}

struct ConnectorStatus: Codable, Identifiable, Equatable, Sendable {
    var id: String { connectorID }
    let connectorID: String
    let provider: String?
    let available: Bool
    let connected: Bool
    let account: String
    let reason: String

    enum CodingKeys: String, CodingKey {
        case connectorID = "connector_id"
        case provider, available, connected, account, reason
    }
}

struct ConnectorSnapshot: Codable, Sendable {
    let connectors: [ConnectorStatus]
}

struct BillingPlan: Codable, Equatable, Sendable {
    let name: String
    let amount: Int
    let currency: String
    let interval: String
}

struct BillingSubscription: Codable, Equatable, Sendable {
    let status: String
    let tier: String
    let cancelAtPeriodEnd: Bool
    let currentPeriodEnd: Int64?

    enum CodingKeys: String, CodingKey {
        case status, tier
        case cancelAtPeriodEnd = "cancel_at_period_end"
        case currentPeriodEnd = "current_period_end"
    }
}

struct BillingSnapshot: Codable, Equatable, Sendable {
    let configured: Bool
    let tier: String
    let customer: Bool
    let subscription: BillingSubscription?
    let plans: [String: BillingPlan]
}

enum ComputerState: String, Codable, Sendable {
    case disabled, pulling, running, hibernated, off, error
}

struct ComputerSnapshot: Codable, Equatable, Sendable {
    let configured: Bool
    let botID: String
    let provider: String?
    let state: ComputerState
    let viewerURL: String
    let viewerExpiresAt: Int64
    let reason: String

    enum CodingKeys: String, CodingKey {
        case configured, provider, state, reason
        case botID = "bot_id"
        case viewerURL = "viewer_url"
        case viewerExpiresAt = "viewer_expires_at"
    }
}

struct BrowserRequest: Identifiable, Equatable {
    enum Purpose: Equatable { case account, connector(String), billing, computer }
    let id = UUID()
    let url: URL
    let purpose: Purpose
}

enum Destination: Hashable {
    case bot(UUID)
    case plugins
    case account
}

extension Color {
    init(hex: String) {
        let clean = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var value: UInt64 = 0
        Scanner(string: clean).scanHexInt64(&value)
        let red, green, blue: UInt64
        if clean.count == 6 {
            red = value >> 16
            green = (value >> 8) & 0xff
            blue = value & 0xff
        } else {
            red = 47; green = 145; blue = 245
        }
        self.init(.sRGB, red: Double(red) / 255, green: Double(green) / 255, blue: Double(blue) / 255)
    }
}

let botColors = ["#a66d35", "#ff2f43", "#ff6a00", "#ff9300", "#08be70", "#11b9a9", "#2f91f5", "#8654ed", "#f35ca7", "#808080"]
