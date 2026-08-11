import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const BROKER_URL_ENV = "PI_CONNECTOR_BROKER_URL";
const RUN_TOKEN_ENV = "PI_CONNECTOR_RUN_TOKEN";
const MAX_RESPONSE_BYTES = 64 * 1024;
const REQUEST_TIMEOUT_MS = 20_000;

const PROVIDERS = Object.freeze([
  provider("google-workspace", "Google Workspace", "Gmail, Calendar, Drive, Contacts y Sheets"),
  provider("slack", "Slack", "canales, mensajes y threads"),
  provider("notion", "Notion", "paginas, bases de datos y conocimiento"),
  provider("salesforce", "Salesforce", "cuentas, contactos y oportunidades"),
  provider("microsoft-365", "Microsoft 365", "Outlook, OneDrive, Calendar y Teams"),
  provider("linkedin", "LinkedIn", "perfiles y relaciones profesionales"),
  provider("zoom", "Zoom", "reuniones y grabaciones"),
  provider("github", "GitHub", "repositorios, issues y pull requests"),
  provider("jira", "Jira", "proyectos, tickets y sprints"),
  provider("linear", "Linear", "issues, proyectos y ciclos"),
  provider("asana", "Asana", "proyectos y tareas"),
  provider("clickup", "ClickUp", "proyectos, tareas y documentos"),
  provider("figma", "Figma", "archivos y comentarios de diseno"),
  provider("hubspot", "HubSpot", "contactos, empresas y oportunidades"),
  provider("canva", "Canva", "disenos y plantillas"),
  provider("trello", "Trello", "tableros, listas y tarjetas"),
  provider("monday-com", "monday.com", "tableros, proyectos e items"),
  provider("intercom", "Intercom", "conversaciones, contactos y soporte"),
  provider("zendesk", "Zendesk", "tickets, usuarios y soporte"),
  provider("box", "Box", "archivos y carpetas empresariales"),
  provider("dropbox", "Dropbox", "archivos y carpetas compartidas"),
  provider("docusign", "DocuSign", "sobres, documentos y firmas"),
  provider("calendly", "Calendly", "eventos, disponibilidad y reuniones"),
  provider("loom", "Loom", "videos y transcripciones"),
  provider("outreach", "Outreach", "prospectos, secuencias y tareas"),
  provider("salesloft", "Salesloft", "personas, cadencias y actividades"),
  provider("apollo", "Apollo", "personas, empresas y enriquecimiento"),
  provider("clay", "Clay", "tablas y enriquecimiento comercial"),
  provider("zoominfo", "ZoomInfo", "contactos e inteligencia comercial"),
  provider("nooks", "Nooks", "sesiones, llamadas y productividad de ventas"),
  provider("stripe", "Stripe", "clientes, pagos, facturas y suscripciones"),
  provider("quickbooks", "QuickBooks", "contabilidad, facturas y gastos"),
  provider("netsuite", "NetSuite", "registros ERP y finanzas"),
  provider("ramp", "Ramp", "tarjetas, transacciones y reembolsos"),
  provider("workday", "Workday", "personas, puestos y ausencias"),
  provider("rippling", "Rippling", "empleados, nomina y dispositivos"),
  provider("ashby", "Ashby", "vacantes, candidatos y entrevistas"),
  provider("greenhouse", "Greenhouse", "vacantes, candidatos y aplicaciones"),
  provider("vercel", "Vercel", "proyectos, deployments y dominios"),
  provider("tableau", "Tableau", "workbooks, dashboards y vistas"),
  provider("hex", "Hex", "proyectos, notebooks y ejecuciones"),
  provider("amplitude", "Amplitude", "eventos, funnels y cohortes"),
  provider("mixpanel", "Mixpanel", "eventos, funnels y retencion"),
  provider("snowflake", "Snowflake", "bases de datos, tablas y consultas"),
  provider("databricks", "Databricks", "lakehouse, notebooks y jobs"),
  provider("mailchimp", "Mailchimp", "audiencias, campanas y automatizaciones"),
  provider("shopify", "Shopify", "productos, pedidos y clientes"),
  provider("tiendanube", "Tiendanube", "productos y pedidos"),
  provider("woocommerce", "WooCommerce", "productos y pedidos de WordPress"),
]);

interface BrokerConnector {
  id: string;
  name: string;
  description: string;
  keywords: string[];
  operations: string[];
  connected: boolean;
}

interface SearchMatch {
  id: string;
  name: string;
  connected: boolean;
  operations: string[];
  tool: string;
}

function provider(id: string, name: string, capabilities: string): {
  id: string;
  name: string;
  capabilities: string;
  toolName: string;
} {
  return Object.freeze({ id, name, capabilities, toolName: `connector_${id.replaceAll("-", "_")}` });
}

function brokerConfig(): { baseUrl: string; token: string } {
  const rawUrl = process.env[BROKER_URL_ENV] ?? "";
  const token = process.env[RUN_TOKEN_ENV] ?? "";
  if (!rawUrl || !token) throw new Error("Los conectores no estan autorizados para esta ejecucion.");
  const url = new URL(rawUrl);
  const loopback = url.hostname === "127.0.0.1" || url.hostname === "[::1]" || url.hostname === "::1";
  if (url.protocol !== "http:" || !loopback || url.username || url.password || url.search || url.hash) {
    throw new Error("El broker de conectores debe ser una URL HTTP loopback sin credenciales.");
  }
  return { baseUrl: url.toString().replace(/\/$/, ""), token };
}

async function readLimited(response: Response): Promise<string> {
  const length = Number(response.headers.get("content-length") ?? "0");
  if (Number.isFinite(length) && length > MAX_RESPONSE_BYTES) {
    throw new Error("La respuesta del conector excedio el limite permitido.");
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_RESPONSE_BYTES) throw new Error("La respuesta del conector excedio el limite permitido.");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(combined);
}

async function brokerRequest(
  path: string,
  init: RequestInit,
  parentSignal?: AbortSignal,
): Promise<{ status: number; ok: boolean; body: unknown }> {
  const { baseUrl, token } = brokerConfig();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new Error("Connector request timed out")), REQUEST_TIMEOUT_MS);
  const abort = () => controller.abort(parentSignal?.reason);
  parentSignal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        ...(init.headers ?? {}),
        "Content-Type": "application/json",
        "X-Connector-Run-Token": token,
      },
    });
    const text = await readLimited(response);
    let body: unknown = {};
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        throw new Error("El broker devolvio una respuesta no valida.");
      }
    }
    return { status: response.status, ok: response.ok, body };
  } finally {
    clearTimeout(timeout);
    parentSignal?.removeEventListener("abort", abort);
  }
}

function errorText(status: number, body: unknown): string {
  if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") {
    return body.error.message;
  }
  return `El broker rechazo la operacion (HTTP ${status}).`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export default function connectorExtension(pi: ExtensionAPI): void {
  const providerToolNames = new Set(PROVIDERS.map((item) => item.toolName));

  for (const item of PROVIDERS) {
    pi.registerTool({
      name: item.toolName,
      label: item.name,
      description: `Usa ${item.name} para ${item.capabilities}. Solo admite operaciones listadas por connector_search.`,
      parameters: Type.Object({
        operation: Type.String({
          description: "Operacion exacta anunciada por connector_search",
          minLength: 1,
          maxLength: 80,
        }),
        arguments: Type.Optional(Type.Record(Type.String(), Type.Unknown(), { description: "Argumentos JSON de la operacion" })),
      }),
      async execute(_toolCallId, params, signal) {
        try {
          const response = await brokerRequest("/v1/internal/connectors/execute", {
            method: "POST",
            body: JSON.stringify({
              connector_id: item.id,
              operation: params.operation,
              arguments: params.arguments ?? {},
            }),
          }, signal);
          if (!response.ok) {
            return {
              content: [{ type: "text", text: errorText(response.status, response.body) }],
              details: { connectorId: item.id, operation: params.operation, status: response.status },
              isError: true,
            };
          }
          return {
            content: [{ type: "text", text: JSON.stringify(response.body) }],
            details: { connectorId: item.id, operation: params.operation, status: response.status },
          };
        } catch (error) {
          return {
            content: [{ type: "text", text: error instanceof Error ? error.message : "Fallo desconocido del conector." }],
            details: { connectorId: item.id, operation: params.operation, status: 0 },
            isError: true,
          };
        }
      },
    });
  }

  pi.registerTool({
    name: "connector_search",
    label: "Connector Search",
    description: "Busca y activa solamente los conectores permitidos para este bot y esta ejecucion.",
    promptSnippet: "Busca conectores cuando una tarea necesite correo, calendario, CRM, trabajo, diseno o ecommerce",
    parameters: Type.Object({
      query: Type.String({
        description: "Capacidad o proveedor necesario, por ejemplo Gmail, calendario, CRM o GitHub",
        minLength: 2,
        maxLength: 200,
      }),
    }),
    async execute(_toolCallId, params, signal) {
      const words = params.query.toLocaleLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
      if (!words.length) {
        return {
          content: [{ type: "text", text: "Especifica una capacidad o proveedor para buscar." }],
          details: { matches: [] as SearchMatch[], activated: [] as string[], status: 400 },
          isError: true,
        };
      }
      try {
        const response = await brokerRequest("/v1/internal/connectors/catalog", { method: "GET" }, signal);
        if (!response.ok) {
          return {
            content: [{ type: "text", text: errorText(response.status, response.body) }],
            details: {
              matches: [] as SearchMatch[],
              activated: [] as string[],
              status: response.status,
            },
            isError: true,
          };
        }
        const connectors = isRecord(response.body) && Array.isArray(response.body.connectors)
          ? response.body.connectors.filter(isBrokerConnector)
          : [];
        const matches = connectors.filter((connector) => {
          const haystack = [connector.id, connector.name, connector.description, ...connector.keywords, ...connector.operations]
            .join(" ").toLocaleLowerCase();
          return words.some((word) => haystack.includes(word));
        });
        const matchedTools = matches
          .flatMap((match): string[] => {
            const tool = PROVIDERS.find((item) => item.id === match.id)?.toolName;
            return tool ? [tool] : [];
          });
        const active = pi.getActiveTools();
        const activated = matchedTools.filter((name) => !active.includes(name));
        if (activated.length) pi.setActiveTools([...active, ...activated]);
        const summary: SearchMatch[] = matches.flatMap((match): SearchMatch[] => {
          const tool = PROVIDERS.find((item) => item.id === match.id)?.toolName;
          return tool ? [{
            id: match.id,
            name: match.name,
            connected: match.connected,
            operations: match.operations,
            tool,
          }] : [];
        });
        return {
          content: [{ type: "text", text: summary.length ? JSON.stringify(summary) : "No hay conectores permitidos que coincidan." }],
          details: { matches: summary, activated, status: response.status },
        };
      } catch (error) {
        return {
          content: [{ type: "text", text: error instanceof Error ? error.message : "No se pudo buscar conectores." }],
          details: { matches: [] as SearchMatch[], activated: [] as string[], status: 0 },
          isError: true,
        };
      }
    },
  });

  pi.on("session_start", () => {
    const active = pi.getActiveTools().filter((name) => !providerToolNames.has(name));
    if (!active.includes("connector_search")) active.push("connector_search");
    pi.setActiveTools(active);
  });
}

function isBrokerConnector(value: unknown): value is BrokerConnector {
  return isRecord(value)
    && typeof value.id === "string"
    && typeof value.name === "string"
    && typeof value.description === "string"
    && Array.isArray(value.keywords) && value.keywords.every((item) => typeof item === "string")
    && Array.isArray(value.operations) && value.operations.every((item) => typeof item === "string")
    && typeof value.connected === "boolean";
}
