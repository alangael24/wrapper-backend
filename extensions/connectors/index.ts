import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync } from "node:fs";

const BROKER_URL_ENV = "PI_CONNECTOR_BROKER_URL";
const RUN_TOKEN_ENV = "PI_CONNECTOR_RUN_TOKEN";
const CONNECTOR_IDS_ENV = "PI_CONNECTOR_IDS";
const COMPUTER_ENABLED_ENV = "PI_COMPUTER_ENABLED";
const AUTH_FILE_ENV = "PI_RUNTIME_AUTH_FILE";
const MAX_RESPONSE_BYTES = 64 * 1024;
const MAX_COMPUTER_RESPONSE_BYTES = 2 * 1024 * 1024;
const REQUEST_TIMEOUT_MS = 45_000;
const COMPUTER_REQUEST_TIMEOUT_MS = 180_000;
const MAX_EAGER_CONNECTORS = 8;

const PROVIDER_OPERATIONS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  "google-workspace": ["search_email", "read_email", "draft_email", "send_email", "list_calendar_events", "create_calendar_event", "delete_calendar_event", "search_drive", "read_drive_file", "list_contacts", "list_sheet_names", "read_sheet", "update_sheet"],
  slack: ["list_channels", "search_messages", "read_thread", "post_message"],
  notion: ["search", "read_page", "create_page", "query_database", "update_page"],
  salesforce: ["search_records", "get_record", "create_record", "update_record"],
  "microsoft-365": ["search_email", "read_email", "draft_email", "list_calendar_events", "create_calendar_event", "search_files", "read_file", "post_teams_message"],
  linkedin: ["get_profile", "search_connections"],
  zoom: ["list_meetings", "get_meeting", "create_meeting", "list_recordings"],
  github: ["search_repositories", "read_file", "list_issues", "get_issue", "create_issue", "list_pull_requests"],
  jira: ["search_issues", "get_issue", "create_issue", "update_issue"],
  linear: ["search_issues", "get_issue", "create_issue", "update_issue"],
  asana: ["search_tasks", "get_task", "create_task", "update_task"],
  clickup: ["search_tasks", "get_task", "create_task", "update_task"],
  figma: ["search_files", "get_file", "list_comments", "post_comment"],
  hubspot: ["search_contacts", "get_contact", "create_contact", "update_contact", "search_deals"],
  canva: ["search_designs", "get_design", "create_design"],
  trello: ["list_boards", "get_board", "list_cards", "create_card", "update_card"],
  "monday-com": ["list_boards", "get_board", "list_items", "create_item", "update_item"],
  intercom: ["search_contacts", "list_conversations", "get_conversation", "reply_conversation"],
  zendesk: ["search_tickets", "get_ticket", "create_ticket", "update_ticket"],
  box: ["search_files", "get_file", "list_folder", "upload_file"],
  dropbox: ["search_files", "get_file", "list_folder", "upload_file"],
  docusign: ["list_envelopes", "get_envelope", "create_envelope", "send_envelope"],
  calendly: ["list_event_types", "list_scheduled_events", "get_event", "cancel_event"],
  loom: ["search_videos", "get_video", "list_transcripts"],
  outreach: ["search_prospects", "get_prospect", "list_sequences", "create_task", "update_prospect"],
  salesloft: ["search_people", "get_person", "list_cadences", "create_activity", "update_person"],
  apollo: ["search_people", "search_organizations", "enrich_person", "enrich_organization"],
  clay: ["list_tables", "get_table", "list_records", "update_record"],
  zoominfo: ["search_contacts", "search_companies", "get_contact", "get_company"],
  nooks: ["list_sessions", "get_session", "list_calls", "get_call"],
  stripe: ["search_customers", "get_customer", "list_payments", "list_invoices", "list_subscriptions"],
  quickbooks: ["search_customers", "get_customer", "list_invoices", "create_invoice", "list_expenses"],
  netsuite: ["search_records", "get_record", "create_record", "update_record"],
  ramp: ["list_cards", "list_transactions", "list_reimbursements", "get_transaction"],
  workday: ["search_workers", "get_worker", "list_positions", "list_time_off"],
  rippling: ["list_employees", "get_employee", "list_payroll_runs", "list_devices"],
  ashby: ["list_jobs", "search_candidates", "get_candidate", "list_interviews"],
  greenhouse: ["list_jobs", "search_candidates", "get_candidate", "list_applications"],
  vercel: ["list_projects", "get_project", "list_deployments", "get_deployment", "list_domains"],
  tableau: ["search_workbooks", "get_workbook", "list_views", "query_view"],
  hex: ["list_projects", "get_project", "run_project", "get_run"],
  amplitude: ["query_events", "query_funnel", "query_retention", "list_cohorts"],
  mixpanel: ["query_events", "query_funnel", "query_retention", "list_profiles"],
  snowflake: ["list_databases", "list_schemas", "list_tables", "describe_table", "select_query", "execute_sql"],
  databricks: ["list_catalogs", "list_schemas", "list_tables", "select_query", "execute_sql", "list_jobs"],
  mailchimp: ["list_audiences", "search_members", "get_campaign", "create_campaign", "list_automations"],
  shopify: ["search_products", "get_product", "list_orders", "get_order", "list_customers"],
  tiendanube: ["search_products", "get_product", "list_orders", "get_order"],
  woocommerce: ["search_products", "get_product", "list_orders", "get_order"],
});

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
  operation_guidance?: Record<string, unknown>;
}

const GOOGLE_WORKSPACE_GUIDANCE = Object.freeze({
  search_email: {
    arguments: {
      query: "Consulta Gmail real; combina términos con OR y usa comillas para frases exactas",
      max_results: "Cantidad máxima de resultados; el backend la limita a 10 por llamada",
      include_content: "true solo cuando necesites extraer el cuerpo de hasta 3 correos probables en esta misma llamada",
    },
    rule: "Ejecuta una búsqueda precisa. Para clasificar o contar usa metadatos; si la tarea necesita datos del cuerpo y la consulta ya es estrecha, usa include_content=true con máximo 3 resultados para evitar otra ronda. Si el cuerpo no aparece, usa read_email para un message_id concreto. Si no hay coincidencias, dilo; nunca inventes asuntos o remitentes.",
  },
  read_email: {
    arguments: {
      message_id: "ID exacto obtenido con search_email",
    },
    rule: "Usa el ID exacto de search_email. No afirmes haber leído un correo si la herramienta falla.",
  },
  search_drive: {
    arguments: {
      query: "Nombre o consulta del archivo; conserva también su ID exacto",
    },
    rule: "Usa el file ID devuelto para cualquier lectura posterior. No confundas el título con el ID.",
  },
  list_sheet_names: {
    arguments: {
      spreadsheet_id: "ID exacto de la hoja de cálculo obtenido con search_drive",
    },
    rule: "Llama esta operación antes de read_sheet o update_sheet cuando no conozcas el nombre real de la pestaña. Usa uno de los nombres devueltos en el rango A1; no adivines Sheet1 ni Hoja 1.",
  },
  read_sheet: {
    arguments: {
      spreadsheet_id: "ID exacto de la hoja obtenido con search_drive",
      range: "Rango pequeño en notación A1, por ejemplo Hoja1!A1:C10",
    },
    rule: "Busca primero el archivo para obtener spreadsheet_id. Mantén el rango pequeño y agrupa celdas adyacentes en una sola lectura. Si la herramienta falla, responde que la lectura falló: nunca presentes celdas como vacías ni inventes valores.",
  },
  update_sheet: {
    arguments: {
      spreadsheet_id: "ID exacto de la hoja obtenido con search_drive",
      range: "Una sola celda o rango pequeño en notación A1",
      values: "Matriz bidimensional de filas; para una celda usa [[valor]] y para vaciarla usa [[\"\"]]",
      value_input_option: "USER_ENTERED por defecto; RAW solo si el usuario lo necesita",
    },
    rule: "Lee primero el rango exacto, no sobrescribas datos no solicitados y usa una sola llamada para celdas adyacentes. Invoca update_sheet directamente para que el backend genere la aprobación estructurada.",
  },
  list_calendar_events: {
    arguments: {
      query: "Texto distintivo del título, descripción, ubicación o asistente; úsalo cuando busques un evento concreto",
      time_min: "Inicio mínimo RFC3339 con offset explícito, por ejemplo 2026-08-25T00:00:00-06:00",
      time_max: "Fin exclusivo RFC3339 con offset explícito; usa una ventana estrecha alrededor de la fecha solicitada",
      calendar_id: "Usa primary salvo que el usuario indique otro calendario",
      max_results: "Máximo de resultados de esta página; mantenlo pequeño",
      page_token: "nextPageToken exacto de la página anterior cuando haya más resultados",
    },
    rule: "Consulta Calendar antes de afirmar qué eventos existen. Para un evento concreto combina query con time_min y time_max, valida título y fecha del resultado, y sigue nextPageToken si existe. Resume solo los eventos devueltos.",
  },
  list_contacts: {
    rule: "No muestres correos ni teléfonos salvo que el usuario los pida explícitamente.",
  },
  draft_email: {
    arguments: {
      recipient_email: "Correo exacto del destinatario",
      subject: "Asunto del borrador",
      body: "Contenido del correo en texto plano",
    },
    rule: "Úsalo solo cuando el usuario pida preparar o guardar un borrador. Nunca afirmes que se envió.",
  },
  send_email: {
    arguments: {
      recipient_email: "Correo exacto del destinatario",
      subject: "Asunto del correo",
      body: "Contenido final del correo en texto plano",
      cc: "Lista opcional de correos en copia",
      bcc: "Lista opcional de correos en copia oculta",
    },
    rule: "Úsalo cuando el usuario pida o confirme explícitamente enviar. No crees un borrador en su lugar. Confirma el envío solo después de una respuesta exitosa.",
  },
  create_calendar_event: {
    arguments: {
      start_datetime: "ISO 8601 exacto, por ejemplo 2026-08-18T15:00:00; no uses texto como 'manana'",
      timezone: "Zona IANA exacta recibida en el contexto, por ejemplo America/Denver",
      summary: "Titulo del evento",
      end_datetime: "ISO 8601 exacto; alternativamente usa event_duration_hour/event_duration_minutes",
      event_duration_hour: "Horas de duracion",
      event_duration_minutes: "Minutos de duracion",
      calendar_id: "Usa primary salvo que el usuario indique otro calendario",
      attendees: "Lista opcional de emails",
    },
    rule: "No afirmes que el evento fue creado hasta que esta herramienta responda sin error. Si el usuario no quiere indicar una hora final, omite end_datetime y la duración para usar el valor seguro predeterminado de una hora; nunca inventes una jornada de 8 horas. Si falla por argumentos, corrige y reintenta.",
  },
  delete_calendar_event: {
    arguments: {
      event_id: "ID exacto del evento; si no lo tienes, usa list_calendar_events primero",
      calendar_id: "Usa primary salvo que el evento pertenezca a otro calendario",
    },
    rule: "Borra únicamente el evento que el usuario pidió eliminar en su turno actual. No confundas event_id con el enlace, título o calendar_id. Confirma solo después de una respuesta exitosa.",
  },
});

const CONNECTOR_GUIDANCE: Readonly<Record<string, Readonly<Record<string, unknown>>>> = Object.freeze({
  "google-workspace": GOOGLE_WORKSPACE_GUIDANCE,
  notion: Object.freeze({
    search: {
      rule: "Busca primero la página o base para obtener su ID exacto. No inventes IDs de Notion.",
    },
    read_page: {
      arguments: { page_id: "ID exacto devuelto por search" },
      rule: "Lee la página antes de resumir su contenido.",
    },
    create_page: {
      arguments: {
        parent_id: "ID exacto de una página padre accesible, obtenido con search",
        title: "Título de la nueva página",
      },
      rule: "La API crea una página vacía bajo un parent_id. Si falta el padre, búscalo o pide solo ese dato. Invoca la escritura para mostrar la aprobación estructurada.",
    },
    update_page: {
      arguments: { page_id: "ID exacto", archived: "boolean opcional", properties: "propiedades opcionales" },
      rule: "No uses update_page para reemplazar el contenido de texto de una página.",
    },
  }),
  canva: Object.freeze({
    search_designs: {
      arguments: { query: "Texto del título del diseño" },
      rule: "Conserva el designId exacto para get_design.",
    },
    get_design: { arguments: { designId: "ID exacto obtenido con search_designs" } },
    create_design: {
      arguments: {
        title: "Título del diseño",
        design_type: "Preset doc, email, presentation o whiteboard; para tamaño libre usa {type:'custom',width,height}",
      },
      rule: "create_design crea un diseño en blanco, no contenido terminado. Invócalo directamente para la aprobación.",
    },
  }),
  "microsoft-365": Object.freeze({
    search_email: {
      arguments: { query: "Consulta de Outlook", size: "1 a 10" },
      rule: "No afirmes haber leído correo si la búsqueda no devuelve mensajes.",
    },
    create_calendar_event: {
      arguments: {
        subject: "Título del evento",
        start_datetime: "ISO 8601 exacto",
        end_datetime: "ISO 8601 exacto; si se omite el backend usa una hora",
        time_zone: "Zona IANA, por ejemplo America/Denver",
        body: "Descripción opcional",
      },
      rule: "No uses nombres de parámetros de Google. Invoca la escritura cuando estén título, inicio y zona horaria para mostrar la aprobación.",
    },
  }),
  figma: Object.freeze({
    search_files: {
      arguments: {
        figma_url: "URL de archivo, diseño, nodo o equipo de Figma",
        team_id: "Alternativa: ID de equipo",
        project_id: "Alternativa: ID de proyecto",
        file_key: "Alternativa: clave de archivo",
      },
      rule: "La API de Figma no ofrece búsqueda global por título. Usa una URL o ID real; si falta, pide únicamente ese dato.",
    },
    get_file: { arguments: { file_key: "Clave exacta obtenida de una URL o search_files" } },
    list_comments: { arguments: { file_key: "Clave exacta del archivo" } },
    post_comment: {
      arguments: { file_key: "Clave exacta", message: "Comentario final" },
      rule: "Invoca directamente para la aprobación estructurada.",
    },
  }),
  calendly: Object.freeze({
    list_event_types: {
      rule: "No pidas el URI del usuario: el backend lo obtiene de la cuenta autenticada.",
    },
    list_scheduled_events: {
      arguments: { min_start_time: "ISO 8601 opcional", max_start_time: "ISO 8601 opcional", count: "1 a 100" },
      rule: "No pidas el URI del usuario: el backend lo obtiene de la cuenta autenticada.",
    },
    get_event: { arguments: { uuid: "UUID o URI exacto del evento listado" } },
    cancel_event: {
      arguments: { uuid: "UUID o URI exacto", reason: "Motivo opcional" },
      rule: "Invoca directamente para mostrar la aprobación estructurada.",
    },
  }),
});

function providerArgumentDescription(id: string): string {
  if (id !== "google-workspace") {
    return "Argumentos JSON según operation_guidance. Usa IDs exactos devueltos por lecturas anteriores; no inventes identificadores ni nombres de parámetros del proveedor.";
  }
  return [
    "Argumentos JSON de la operacion.",
    "Para search_email usa query con sintaxis de Gmail y max_results; activa include_content solo en búsquedas estrechas de hasta 3 resultados. Para read_email usa message_id obtenido de la búsqueda cuando el contenido no vino.",
    "Para draft_email y send_email usa recipient_email, subject y body; send_email envía realmente y draft_email solo guarda.",
    "Para create_calendar_event usa start_datetime ISO 8601 exacto, timezone IANA, summary y",
    "end_datetime o event_duration_hour/event_duration_minutes; calendar_id normalmente es primary.",
    "Para delete_calendar_event usa event_id exacto y calendar_id; lista eventos primero si falta el ID.",
    "Para search_drive usa query. Antes de leer o escribir una hoja cuyo nombre de pestaña no conozcas, usa list_sheet_names con el spreadsheet_id. Para read_sheet usa spreadsheet_id y un range pequeño en notación A1 con el nombre real devuelto.",
    "Para update_sheet usa spreadsheet_id, range, values como matriz bidimensional y value_input_option USER_ENTERED.",
    "Nunca pases fechas naturales como 'tomorrow' o 'manana'.",
  ].join(" ");
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
  const token = currentConnectorToken();
  if (!rawUrl || !token) throw new Error("Los conectores no estan autorizados para esta ejecucion.");
  const url = new URL(rawUrl);
  const loopback = url.hostname === "127.0.0.1" || url.hostname === "[::1]" || url.hostname === "::1";
  if (url.protocol !== "http:" || !loopback || url.username || url.password || url.search || url.hash) {
    throw new Error("El broker de conectores debe ser una URL HTTP loopback sin credenciales.");
  }
  return { baseUrl: url.toString().replace(/\/$/, ""), token };
}

function currentConnectorToken(): string {
  return currentRuntimeGrant().token;
}

interface RuntimeGrant {
  token: string;
  connectorIds: string[];
  computerEnabled: boolean;
}

function currentRuntimeGrant(): RuntimeGrant {
  const authFile = process.env[AUTH_FILE_ENV] ?? "";
  if (authFile) {
    try {
      const parsed = JSON.parse(readFileSync(authFile, "utf8")) as {
        connector_run_token?: unknown;
        connector_ids?: unknown;
        computer_enabled?: unknown;
      };
      return {
        token: typeof parsed.connector_run_token === "string" ? parsed.connector_run_token : "",
        connectorIds: Array.isArray(parsed.connector_ids)
          ? [...new Set(parsed.connector_ids.filter((item): item is string => typeof item === "string"))]
          : [],
        computerEnabled: parsed.computer_enabled === true,
      };
    } catch {
      return { token: "", connectorIds: [], computerEnabled: false };
    }
  }
  let connectorIds: string[] = [];
  try {
    const parsed = JSON.parse(process.env[CONNECTOR_IDS_ENV] ?? "[]") as unknown;
    if (Array.isArray(parsed)) {
      connectorIds = [...new Set(parsed.filter((item): item is string => typeof item === "string"))];
    }
  } catch {
    connectorIds = [];
  }
  return {
    token: process.env[RUN_TOKEN_ENV] ?? "",
    connectorIds,
    computerEnabled: process.env[COMPUTER_ENABLED_ENV] === "1",
  };
}

async function readLimited(response: Response, maxBytes = MAX_RESPONSE_BYTES): Promise<string> {
  const length = Number(response.headers.get("content-length") ?? "0");
  if (Number.isFinite(length) && length > maxBytes) {
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
      if (total > maxBytes) throw new Error("La respuesta del conector excedio el limite permitido.");
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
  const timeoutMs = path.startsWith("/v1/internal/computers/")
    ? COMPUTER_REQUEST_TIMEOUT_MS
    : REQUEST_TIMEOUT_MS;
  const timeout = setTimeout(() => controller.abort(new Error("Broker request timed out")), timeoutMs);
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
    const text = await readLimited(
      response,
      path.startsWith("/v1/internal/computers/") ? MAX_COMPUTER_RESPONSE_BYTES : MAX_RESPONSE_BYTES,
    );
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

  const applyGrantedTools = (): void => {
    const grant = currentRuntimeGrant();
    const base = pi.getActiveTools().filter((name) => (
      !providerToolNames.has(name)
      && name !== "computer"
      && name !== "connector_search"
    ));
    if (!grant.token) {
      pi.setActiveTools(base);
      return;
    }
    const allowed = grant.connectorIds.filter((id) => PROVIDER_OPERATIONS[id]);
    if (allowed.length > 0 && allowed.length <= MAX_EAGER_CONNECTORS) {
      for (const id of allowed) {
        const tool = PROVIDERS.find((item) => item.id === id)?.toolName;
        if (tool && !base.includes(tool)) base.push(tool);
      }
    } else if (
      (allowed.length > MAX_EAGER_CONNECTORS || !grant.computerEnabled)
      && !base.includes("connector_search")
    ) {
      // Large connector sets stay lazy so the provider does not receive an
      // unnecessarily large tool schema on every model round.
      base.push("connector_search");
    }
    if (grant.computerEnabled && !base.includes("computer")) base.push("computer");
    pi.setActiveTools(base);
  };

  for (const item of PROVIDERS) {
    const operations = PROVIDER_OPERATIONS[item.id] ?? [];
    pi.registerTool({
      name: item.toolName,
      label: item.name,
      description: [
        `Usa ${item.name} para ${item.capabilities}.`,
        "Solo admite operaciones del catálogo autorizado.",
        CONNECTOR_GUIDANCE[item.id]
          ? `Contratos semánticos: ${JSON.stringify(CONNECTOR_GUIDANCE[item.id])}`
          : "Usa IDs exactos devueltos por lecturas previas y no inventes parámetros.",
      ].join(" "),
      parameters: Type.Object({
        operation: Type.String({
          description: "Operacion exacta anunciada por connector_search",
          enum: [...operations],
        }),
        arguments: Type.Optional(Type.Record(Type.String(), Type.Unknown(), {
          description: providerArgumentDescription(item.id),
        })),
      }),
      async execute(toolCallId, params, signal) {
        try {
          const response = await brokerRequest("/v1/internal/connectors/execute", {
            method: "POST",
            body: JSON.stringify({
              connector_id: item.id,
              operation: params.operation,
              arguments: params.arguments ?? {},
              operation_id: toolCallId,
            }),
          }, signal);
          if (!response.ok) {
            return {
              content: [{ type: "text", text: `CONNECTOR_OPERATION_FAILED: ${errorText(response.status, response.body)} No infieras que no existen datos ni afirmes que una acción se completó.` }],
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
    description: "Busca y activa solamente los conectores o la computadora permitidos para este bot y esta ejecucion.",
    promptSnippet: "Busca conectores o computadora cuando una tarea necesite correo, calendario, CRM, GUI, archivos, shell o ecommerce",
    parameters: Type.Object({
      query: Type.String({
        description: "Capacidad o proveedor necesario, por ejemplo Gmail, calendario, CRM o GitHub",
        minLength: 2,
        maxLength: 200,
      }),
    }),
    async execute(_toolCallId, params, signal) {
      const words = params.query.toLocaleLowerCase().split(/[^\p{L}\p{N}]+/u)
        .filter((word) => word.length >= 2);
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
        const computerAvailable = isRecord(response.body) && response.body.computer === true;
        const wantsComputer = words.some((word) => [
          "computer", "computadora", "desktop", "escritorio", "gui", "screen", "pantalla",
          "shell", "terminal", "navegador", "browser",
        ].includes(word));
        const matches = connectors.filter((connector) => {
          if (!connector.connected) return false;
          const tokens = new Set(
            [connector.id, connector.name, connector.description, ...connector.keywords, ...connector.operations]
              .join(" ").toLocaleLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean),
          );
          return words.some((word) => tokens.has(word)
            || (word.length >= 5 && [...tokens].some((token) => token.length >= 5
              && (token.startsWith(word) || word.startsWith(token)))));
        });
        const matchedTools = matches
          .flatMap((match): string[] => {
            const tool = PROVIDERS.find((item) => item.id === match.id)?.toolName;
            return tool ? [tool] : [];
          });
        const active = pi.getActiveTools();
        if (computerAvailable && wantsComputer && !matchedTools.includes("computer")) matchedTools.push("computer");
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
            ...(CONNECTOR_GUIDANCE[match.id] ? { operation_guidance: CONNECTOR_GUIDANCE[match.id] } : {}),
          }] : [];
        });
        if (computerAvailable && wantsComputer) {
          summary.unshift({
            id: "computer",
            name: "Agent Computer",
            connected: true,
            operations: ["screenshot", "click", "type", "shell", "list_files", "read_file", "write_file"],
            tool: "computer",
          });
        }
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

  pi.registerTool({
    name: "computer",
    label: "Agent Computer",
    description: [
      "Controla la computadora persistente y aislada de este bot.",
      "Operaciones: status, screenshot, click, move, drag, scroll, type, key, hotkey, shell, list_files, read_file, write_file.",
      "Argumentos: click{x,y,button?,double?}; move{x,y}; drag{start_x,start_y,end_x,end_y,button?};",
      "scroll{x,y,direction,amount?}; type{text,delay?}; key{key,modifiers?}; hotkey{keys};",
      "shell{command,cwd?,timeout?}; list_files{path?,depth?}; read_file{path}; write_file{path,content}.",
      "Usa screenshot antes de hacer clic y vuelve a capturar después de cada cambio importante.",
    ].join(" "),
    promptSnippet: "Usa computer cuando una tarea necesite una GUI, archivos o shell en la computadora persistente del bot",
    parameters: Type.Object({
      operation: Type.String({
        description: "Una operación exacta de la lista documentada",
        minLength: 3,
        maxLength: 40,
      }),
      arguments: Type.Optional(Type.Record(Type.String(), Type.Unknown(), {
        description: "Argumentos JSON correspondientes a la operación",
      })),
    }),
    async execute(_toolCallId, params, signal) {
      try {
        const response = await brokerRequest("/v1/internal/computers/execute", {
          method: "POST",
          body: JSON.stringify({ operation: params.operation, arguments: params.arguments ?? {} }),
        }, signal);
        if (!response.ok) {
          return {
            content: [{ type: "text", text: errorText(response.status, response.body) }],
            details: { operation: params.operation, status: response.status },
            isError: true,
          };
        }
        const result = isRecord(response.body) && isRecord(response.body.result)
          ? response.body.result
          : null;
        if (params.operation === "screenshot" && result
          && typeof result.image_base64 === "string"
          && typeof result.mime_type === "string") {
          return {
            content: [
              { type: "text", text: "Captura actual de la computadora del bot." },
              { type: "image", data: result.image_base64, mimeType: result.mime_type },
            ],
            details: { operation: params.operation, status: response.status },
          };
        }
        return {
          content: [{ type: "text", text: JSON.stringify(response.body) }],
          details: { operation: params.operation, status: response.status },
        };
      } catch (error) {
        return {
          content: [{ type: "text", text: error instanceof Error ? error.message : "No se pudo controlar la computadora." }],
          details: { operation: params.operation, status: 0 },
          isError: true,
        };
      }
    },
  });

  pi.on("session_start", applyGrantedTools);
  // Warm Pi processes rotate grants between turns. Re-read the atomic runtime
  // file immediately before each agent loop so the first provider request can
  // call the exact authorized connector directly, without a discovery round.
  pi.on("before_agent_start", applyGrantedTools);
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
