#!/usr/bin/env electron

import { app, BrowserWindow, safeStorage } from "electron";
import { randomUUID } from "node:crypto";
import { readFile, rename, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

app.setName("Agent Genia");

const API = (process.env.AGENTGENIA_API_BASE_URL || "https://agentgenia-api.onrender.com").replace(/\/$/, "");
const USER_DATA = process.env.AGENTGENIA_USER_DATA_DIR
  || path.join(os.homedir(), "Library", "Application Support", "Agent Genia");
const RUN_PREFIX = `e2e-${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}-${randomUUID().slice(0, 8)}`;
const results = [];

function record(name, difficulty, started, ok, detail = "") {
  results.push({ name, difficulty, ok, duration_ms: Math.round(performance.now() - started), detail });
}

function assert(value, message) {
  if (!value) throw new Error(message);
}

function assertConnectorSucceeded(answer) {
  assert(answer.text.length > 0, "empty connector answer");
  assert(
    !/(?:no puedo|no pude|no logró|no tengo acceso|pega el contenido|falló (?:la|el)|error (?:al|de)|requiere (?:un |una )?(?:parámetro|campo|query|consulta)|no (?:está|estuvo) disponible)/i.test(answer.text),
    "connector answer reported an operational failure"
  );
}

async function decrypt(file) {
  const encrypted = await readFile(file);
  return (await safeStorage.decryptStringAsync(encrypted)).result;
}

async function saveEncrypted(file, value) {
  const temporary = `${file}.${process.pid}.tmp`;
  await writeFile(temporary, await safeStorage.encryptStringAsync(value), { mode: 0o600, flag: "wx" });
  await rename(temporary, file);
  await rm(temporary, { force: true });
}

async function responseJson(response) {
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch {}
  if (!response.ok) {
    const message = payload?.error?.message || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function request(route, token, options = {}) {
  return responseJson(await fetch(`${API}${route}`, {
    method: options.method || "GET",
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: options.body ? JSON.stringify(options.body) : undefined
  }));
}

function processSseFrame(frame, state) {
  let event = "message";
  const data = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  const payload = data.join("\n");
  if (event === "start") state.runId = JSON.parse(payload).run_id || state.runId;
  else if (event === "delta") {
    if (!state.firstDeltaAt) state.firstDeltaAt = performance.now();
    state.streamed += JSON.parse(payload).text || "";
  } else if (event === "done64") {
    state.final = { answer: Buffer.from(payload.trim(), "base64").toString("utf8") };
  } else if (event === "done") state.final = JSON.parse(payload);
  else if (event === "error") {
    const errorPayload = JSON.parse(payload);
    const error = new Error(errorPayload.message || "Agent run failed");
    error.status = errorPayload.status;
    error.payload = errorPayload;
    throw error;
  }
}

async function runAgent(token, body) {
  const started = performance.now();
  const response = await fetch(`${API}/v1/agent/run`, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`
    },
    body: JSON.stringify({ max_credits: 15, stream: true, client_timezone: "America/Denver", ...body })
  });
  if (!response.ok) await responseJson(response);
  const state = { runId: response.headers.get("x-agent-run-id") || "", streamed: "", final: null, firstDeltaAt: 0 };
  assert(response.body, "stream has no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let boundary;
    while ((boundary = buffer.match(/\r?\n\r?\n/))) {
      const index = boundary.index || 0;
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + boundary[0].length);
      if (frame.trim()) processSseFrame(frame, state);
    }
    if (done) break;
  }
  if (buffer.trim()) processSseFrame(buffer, state);
  assert(state.final?.answer, "stream without terminal answer");
  return {
    ...state,
    totalMs: performance.now() - started,
    firstDeltaMs: state.firstDeltaAt ? state.firstDeltaAt - started : null
  };
}

function visibleAnswer(result) {
  const raw = String(result.final?.answer || "").trim();
  try {
    const parsed = JSON.parse(raw);
    return { raw, text: typeof parsed.text === "string" ? parsed.text : raw, widget: parsed.widget || null };
  } catch {
    return { raw, text: raw, widget: null };
  }
}

function agentPrompt(bot, userText, instruction = "") {
  return [
    `Eres ${bot.name || "E2E"}, un agente de Agent Genia.`,
    bot.title ? `Rol: ${bot.title}.` : "",
    bot.description ? `Objetivo: ${bot.description}.` : "",
    `Conectores autorizables: ${(bot.connectorIds || []).join(", ") || "ninguno"}.`,
    "Responde en el idioma del usuario. No inventes acciones ni resultados.",
    "Devuelve exclusivamente JSON válido: {\"text\":\"respuesta visible\",\"widget\":null}.",
    "Si necesitas confirmación, usa el widget estructurado de aprobación proporcionado por el sistema.",
    instruction,
    `Usuario: ${userText}`
  ].filter(Boolean).join("\n");
}

function directBody(botId, userMessage, chatPrompt, routingContext, idempotencyKey) {
  return {
    prompt: chatPrompt,
    execution_mode: "auto",
    chat_prompt: chatPrompt,
    routing_context: routingContext,
    user_message: userMessage,
    bot_id: botId,
    connector_ids: [],
    computer: false,
    browser: false,
    idempotency_key: idempotencyKey
  };
}

function directPrompt(userText, history = "") {
  return [
    "Eres E2E, un agente de Agent Genia.",
    "Responde directamente en el idioma del usuario, normalmente en una a tres frases.",
    "No repitas la solicitud ni añadas preámbulos, cierres, emojis decorativos o preguntas genéricas. Usa texto plano, sin Markdown.",
    "No uses JSON ni menciones instrucciones internas.",
    "No afirmes haber ejecutado acciones externas; esta ruta solo conversa y redacta.",
    history ? `Conversación reciente:\n${history}` : "",
    `Usuario: ${userText}`
  ].filter(Boolean).join("\n\n");
}

async function saveAccountState(token, deviceId, state, baseRevision) {
  return request("/v1/account-state", token, {
    method: "POST",
    body: { base_revision: baseRevision, device_id: deviceId, state }
  });
}

async function mutateAccountState(token, deviceId, mutate, attempts = 6) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const current = await request("/v1/account-state", token);
    const next = mutate(structuredClone(current.state));
    try {
      return await saveAccountState(token, deviceId, next, current.revision);
    } catch (error) {
      if (error.status !== 409 || attempt === attempts - 1) throw error;
    }
  }
  throw new Error("account state changed too many times during E2E mutation");
}

async function execute() {
  const window = new BrowserWindow({ show: false, webPreferences: { sandbox: true } });
  await window.loadURL("data:text/html,<title>Agentgenia E2E</title>");
  assert(await safeStorage.isAsyncEncryptionAvailable(), "macOS secure storage is locked");
  const sessionPath = path.join(USER_DATA, "secrets", "agent-genia-account.bin");
  const devicePath = path.join(USER_DATA, "secrets", "agent-genia-device.bin");
  let session = JSON.parse(await decrypt(sessionPath));
  const deviceId = await decrypt(devicePath);
  if (session.expiresAt < Date.now() + 120_000) {
    const refreshed = await request("/v1/account-auth/refresh", session.refreshToken, {
      method: "POST", body: { device_id: deviceId }
    });
    session = {
      ...session,
      token: refreshed.token,
      refreshToken: refreshed.refresh_token || session.refreshToken,
      expiresAt: refreshed.expires_at
    };
    await saveEncrypted(sessionPath, JSON.stringify(session));
  }
  const token = session.token;

  let started = performance.now();
  try {
    const ready = await request("/readyz", null);
    assert(ready.ready === true, "production readiness is false");
    record("production readiness", "simple", started, true);
  } catch (error) { record("production readiness", "simple", started, false, error.message); }

  let me, connectors, accountState, billing, whatsapp;
  started = performance.now();
  try {
    [me, connectors, accountState, billing, whatsapp] = await Promise.all([
      request("/v1/me", token), request("/v1/connectors", token), request("/v1/account-state", token),
      request("/v1/billing", token), request("/v1/whatsapp/status", token)
    ]);
    assert(me?.user_id || me?.id, "profile missing account id");
    assert(Array.isArray(connectors?.connectors), "connector snapshot invalid");
    assert(accountState?.state && Array.isArray(accountState.state.bots), "account state invalid");
    assert(typeof billing?.configured === "boolean", "billing snapshot invalid");
    assert(typeof whatsapp?.configured === "boolean", "WhatsApp snapshot invalid");
    record("account surfaces agree", "simple", started, true);
  } catch (error) {
    record("account surfaces agree", "simple", started, false, error.message);
    throw error;
  }

  for (const [name, route] of [
    ["profile snapshot latency", "/v1/me"],
    ["connector snapshot latency", "/v1/connectors"],
    ["account state latency", "/v1/account-state"],
    ["billing snapshot latency", "/v1/billing"],
    ["WhatsApp snapshot latency", "/v1/whatsapp/status"]
  ]) {
    started = performance.now();
    try {
      await request(route, token);
      record(name, "simple", started, true);
    } catch (error) { record(name, "simple", started, false, error.message); }
  }

  const testBotId = randomUUID();
  for (let index = 1; index <= 5; index += 1) {
    const marker = `OK${index}-${randomUUID().slice(0, 6)}`;
    started = performance.now();
    try {
      const run = await runAgent(token, directBody(
        testBotId,
        `Responde únicamente ${marker}`,
        directPrompt(`Responde únicamente ${marker}`),
        "",
        `${RUN_PREFIX}-simple-${index}`
      ));
      const answer = visibleAnswer(run).text.trim();
      assert(answer === marker, `unexpected exact answer (${answer.length} chars)`);
      record(`exact chat ${index}`, "simple", started, true, `total=${Math.round(run.totalMs)} first=${Math.round(run.firstDeltaMs || 0)}`);
    } catch (error) { record(`exact chat ${index}`, "simple", started, false, error.message); }
  }

  const memoryMarker = `MEM-${randomUUID().slice(0, 8)}`;
  started = performance.now();
  try {
    const first = await runAgent(token, directBody(
      testBotId, `Recuerda ${memoryMarker} y responde guardado.`,
      directPrompt(`Recuerda ${memoryMarker} y responde guardado.`), "", `${RUN_PREFIX}-memory-1`
    ));
    const firstText = visibleAnswer(first).text;
    const second = await runAgent(token, directBody(
      testBotId, "¿Cuál era la clave? Responde solo la clave.",
      directPrompt("¿Cuál era la clave? Responde solo la clave.", `Usuario: Recuerda ${memoryMarker} y responde guardado.\nE2E: ${firstText}`),
      `Usuario: Recuerda ${memoryMarker}.\nAgente: ${firstText}`,
      `${RUN_PREFIX}-memory-2`
    ));
    assert(visibleAnswer(second).text.includes(memoryMarker), "multi-turn memory mismatch");
    record("client-synchronized multi-turn context", "medium", started, true);
  } catch (error) { record("client-synchronized multi-turn context", "medium", started, false, error.message); }

  started = performance.now();
  try {
    const key = `${RUN_PREFIX}-idempotent`;
    const body = directBody(testBotId, "Responde solo IDEMPOTENTE", directPrompt("Responde solo IDEMPOTENTE"), "", key);
    const first = await runAgent(token, body);
    const second = await runAgent(token, body);
    assert(first.runId && first.runId === second.runId, "idempotency created a second run");
    assert(visibleAnswer(first).text === visibleAnswer(second).text, "idempotent replay changed result");
    record("durable idempotency replay", "medium", started, true);
  } catch (error) { record("durable idempotency replay", "medium", started, false, error.message); }

  started = performance.now();
  try {
    const runs = await Promise.all(Array.from({ length: 4 }, (_, index) => {
      const marker = `P${index}-${randomUUID().slice(0, 5)}`;
      return runAgent(token, directBody(randomUUID(), `Responde solo ${marker}`, directPrompt(`Responde solo ${marker}`), "", `${RUN_PREFIX}-parallel-${index}`))
        .then((run) => visibleAnswer(run).text.trim() === marker);
    }));
    assert(runs.every(Boolean), "one parallel response was wrong");
    record("four independent concurrent chats", "medium", started, true);
  } catch (error) { record("four independent concurrent chats", "medium", started, false, error.message); }

  if (process.env.AGENTGENIA_E2E_BROWSER === "1") {
    const browserCases = [
      ["local Chrome exact page read", "medium", "Usa Chrome local. Abre https://example.com y responde únicamente con el título visible de la página."],
      ["local Chrome two-page comparison", "hard", "Usa Chrome local. Abre https://example.com y https://www.iana.org/help/example-domains. Compara en dos frases para qué sirven los dominios de ejemplo según esas páginas."],
      ["local Chrome product navigation", "hard", "Usa Chrome local. Abre https://agentgenia.com, identifica el encabezado principal visible y resume en una frase qué ofrece el producto. No uses conocimiento previo: inspecciona la página."]
    ];
    for (let index = 0; index < browserCases.length; index += 1) {
      const [name, difficulty, userText] = browserCases[index];
      started = performance.now();
      try {
        const run = await runAgent(token, {
          prompt: agentPrompt({ name: "Browser E2E", connectorIds: [] }, userText),
          execution_mode: "agent", chat_prompt: "", routing_context: "", user_message: userText,
          bot_id: testBotId, connector_ids: [], computer: true, browser: true,
          idempotency_key: `${RUN_PREFIX}-browser-${index}`
        });
        const answer = visibleAnswer(run).text;
        assert(answer.length > 0, "empty browser answer");
        assert(!/abre agent genia|no puedo|no está disponible|offline/i.test(answer), "local Chrome was not executed");
        if (index === 0) assert(/Example Domain/i.test(answer), "wrong example.com title");
        record(name, difficulty, started, true, `total=${Math.round(run.totalMs)} first=${Math.round(run.firstDeltaMs || 0)}`);
      } catch (error) { record(name, difficulty, started, false, error.message); }
    }
  }

  const connected = connectors.connectors.filter((item) => item.connected).map((item) => item.connector_id);
  const existingBot = accountState.state.bots.find((item) => (item.connectorIds || []).some((id) => connected.includes(id)))
    || accountState.state.bots.find((item) => item.id === accountState.state.activeBotId)
    || accountState.state.bots[0];
  let bot = existingBot;
  let scoped = existingBot ? (existingBot.connectorIds || []).filter((id) => connected.includes(id)) : [];
  let temporaryBotId = "";
  if (existingBot && connected.length > scoped.length) {
    temporaryBotId = randomUUID();
    const now = new Date().toISOString();
    const temporaryBot = {
      ...existingBot,
      id: temporaryBotId,
      name: "Reliability E2E",
      title: "Auditor de conectores",
      description: "Ejecuta pruebas de lectura controladas sin modificar datos.",
      connectorIds: connected,
      messages: [],
      workflows: [],
      createdAt: now,
      updatedAt: now,
      profileRevision: now,
      connectorAssignmentRevision: now,
      notificationRevision: now,
      conversationRevision: now,
      workflowRevision: now
    };
    accountState = await mutateAccountState(token, deviceId, (state) => ({
      ...state,
      bots: state.bots.some((item) => item.id === temporaryBot.id)
        ? state.bots
        : [...state.bots, temporaryBot]
    }));
    bot = temporaryBot;
    scoped = connected;
  }
  record("connected connector inventory", "simple", performance.now(), connected.length > 0, `${connected.length} connected; ${scoped.length} assigned for E2E`);

  if (bot && scoped.includes("google-workspace")) {
    const connectorCases = [
      ["gmail bounded read", "medium", "Busca en Gmail hasta 5 correos recientes que contengan CDL. Si no existe ninguno, dilo claramente. No hagas cambios."],
      ["calendar bounded read", "medium", "Lista como máximo 3 eventos próximos de mi calendario principal. No hagas cambios."],
      ["cross-source travel audit", "hard", "Busca hasta 10 correos recientes que parezcan confirmaciones de vuelos. Extrae fechas y destinos cuando existan y compáralos con mi calendario para señalar faltantes o conflictos. No crees, edites ni elimines nada."]
    ];
    for (let index = 0; index < connectorCases.length; index += 1) {
      const [name, difficulty, userText] = connectorCases[index];
      started = performance.now();
      try {
        const run = await runAgent(token, {
          prompt: agentPrompt(bot, userText), execution_mode: "agent", chat_prompt: "", routing_context: "",
          user_message: userText, bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
          idempotency_key: `${RUN_PREFIX}-connector-${index}`
        });
        const answer = visibleAnswer(run);
        assertConnectorSucceeded(answer);
        record(name, difficulty, started, true, `total=${Math.round(run.totalMs)} first=${Math.round(run.firstDeltaMs || 0)}`);
      } catch (error) { record(name, difficulty, started, false, error.message); }
    }

    started = performance.now();
    try {
      const title = `Agentgenia E2E cancel ${RUN_PREFIX}`;
      const userText = `Crea un evento titulado "${title}" el 30 de agosto de 2026 de 10:00 a 10:15 en mi calendario principal.`;
      const proposed = await runAgent(token, {
        prompt: agentPrompt(bot, userText), execution_mode: "agent", chat_prompt: "", routing_context: "",
        user_message: userText, bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
        idempotency_key: `${RUN_PREFIX}-approval-propose`
      });
      const envelope = visibleAnswer(proposed);
      assert(envelope.widget?.type === "approval", "write did not return an approval widget");
      const approvalId = envelope.widget.approvalId;
      const rejected = await runAgent(token, {
        prompt: "Cancelar esta acción", execution_mode: "agent", chat_prompt: "", routing_context: "",
        user_message: "Cancelar", bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
        approval: { approval_id: approvalId, decision: "reject" }, idempotency_key: `${RUN_PREFIX}-approval-reject`
      });
      assert(/cancelada|no se realizó/i.test(visibleAnswer(rejected).text), "rejection was not confirmed");
      record("structured write approval rejection", "hard", started, true);
    } catch (error) { record("structured write approval rejection", "hard", started, false, error.message); }

    if (process.env.AGENTGENIA_E2E_MUTATIONS === "1") {
      started = performance.now();
      const title = `Agentgenia E2E lifecycle ${RUN_PREFIX}`;
      try {
        const createText = `Crea un evento titulado "${title}" el 30 de agosto de 2026 de 11:00 a 11:15 en mi calendario principal.`;
        const createProposal = await runAgent(token, {
          prompt: agentPrompt(bot, createText), execution_mode: "agent", chat_prompt: "", routing_context: "",
          user_message: createText, bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
          idempotency_key: `${RUN_PREFIX}-lifecycle-create-propose`
        });
        const createEnvelope = visibleAnswer(createProposal);
        assert(createEnvelope.widget?.type === "approval", "calendar create did not request approval");
        const created = await runAgent(token, {
          prompt: "Autorizar esta acción", execution_mode: "agent", chat_prompt: "", routing_context: "",
          user_message: "Autorizar", bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
          approval: { approval_id: createEnvelope.widget.approvalId, decision: "approve" },
          idempotency_key: `${RUN_PREFIX}-lifecycle-create-approve`
        });
        assert(/creé|cree|evento/i.test(visibleAnswer(created).text), "calendar creation was not confirmed");

        const deleteText = `Elimina exactamente el evento "${title}" del 30 de agosto de 2026 de mi calendario principal.`;
        const deleteProposal = await runAgent(token, {
          prompt: agentPrompt(bot, deleteText), execution_mode: "agent", chat_prompt: "", routing_context: "",
          user_message: deleteText, bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
          idempotency_key: `${RUN_PREFIX}-lifecycle-delete-propose`
        });
        const deleteEnvelope = visibleAnswer(deleteProposal);
        assert(deleteEnvelope.widget?.type === "approval", "calendar delete did not request approval");
        const deleted = await runAgent(token, {
          prompt: "Autorizar esta acción", execution_mode: "agent", chat_prompt: "", routing_context: "",
          user_message: "Autorizar", bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
          approval: { approval_id: deleteEnvelope.widget.approvalId, decision: "approve" },
          idempotency_key: `${RUN_PREFIX}-lifecycle-delete-approve`
        });
        assert(/eliminé|elimine|eliminado/i.test(visibleAnswer(deleted).text), "calendar deletion was not confirmed");
        record("calendar create-approve-delete lifecycle", "hard", started, true);
      } catch (error) {
        record("calendar create-approve-delete lifecycle", "hard", started, false, error.message);
      }
    }
  }

  const additionalConnectorCases = [
    ["notion", "Notion bounded search", "Busca en Notion hasta 5 páginas relacionadas con Agentgenia. No hagas cambios."],
    ["microsoft-365", "Microsoft 365 bounded read", "Busca en Outlook hasta 5 correos recientes. Resume solo remitente y asunto; no hagas cambios."],
    ["figma", "Figma bounded search", "Busca hasta 5 archivos de Figma accesibles. No publiques comentarios ni hagas cambios."],
    ["canva", "Canva bounded search", "Busca hasta 5 diseños recientes de Canva. No crees ni modifiques diseños."],
    ["calendly", "Calendly bounded read", "Lista hasta 5 tipos de evento de Calendly. No canceles ni cambies nada."]
  ];
  for (let index = 0; index < additionalConnectorCases.length; index += 1) {
    const [connectorId, name, userText] = additionalConnectorCases[index];
    if (!bot || !scoped.includes(connectorId)) continue;
    started = performance.now();
    try {
      const run = await runAgent(token, {
        prompt: agentPrompt(bot, userText), execution_mode: "agent", chat_prompt: "", routing_context: "",
        user_message: userText, bot_id: bot.id, connector_ids: scoped, computer: false, browser: false,
        idempotency_key: `${RUN_PREFIX}-extra-${index}`
      });
      const answer = visibleAnswer(run);
      assertConnectorSucceeded(answer);
      record(name, "medium", started, true, `total=${Math.round(run.totalMs)} first=${Math.round(run.firstDeltaMs || 0)}`);
    } catch (error) { record(name, "medium", started, false, error.message); }
  }

  if (temporaryBotId) {
    started = performance.now();
    try {
      await mutateAccountState(token, deviceId, (state) => ({
        ...state,
        bots: state.bots.filter((item) => item.id !== temporaryBotId),
        deletedBotIds: [...new Set([...(state.deletedBotIds || []), temporaryBotId])],
        activeBotId: state.activeBotId === temporaryBotId ? (existingBot?.id || null) : state.activeBotId
      }));
      record("temporary E2E bot cleanup", "simple", started, true);
    } catch (error) { record("temporary E2E bot cleanup", "simple", started, false, error.message); }
  }

  window.destroy();
  const passed = results.filter((item) => item.ok).length;
  const failed = results.length - passed;
  console.log(JSON.stringify({ run: RUN_PREFIX, passed, failed, connected, scoped, results }, null, 2));
  if (failed) process.exitCode = 1;
}

app.whenReady().then(execute).then(() => {
  app.exit(process.exitCode || 0);
}).catch((error) => {
  console.error(JSON.stringify({ fatal: error.message }));
  app.exit(1);
});
