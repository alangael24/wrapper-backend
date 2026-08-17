import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { build } from "esbuild";


test("connector_search activa solo la herramienta necesaria y el broker recibe el grant", async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), "wrapper-connector-extension-"));
  const output = path.join(temporary, "extension.mjs");
  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null;
    requests.push({ method: request.method, url: request.url, token: request.headers["x-connector-run-token"], body });
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/v1/internal/connectors/catalog") {
      response.end(JSON.stringify({
        computer: true,
        connectors: [
          {
            id: "github",
            name: "GitHub",
            description: "Repositorios, issues y pull requests.",
            keywords: ["github", "repo", "code"],
            operations: ["search_repositories"],
            connected: true,
          },
          {
            id: "google-workspace",
            name: "Google Workspace",
            description: "Gmail y Calendar.",
            keywords: ["gmail", "calendar"],
            operations: ["search_email", "create_calendar_event"],
            connected: true,
          },
        ],
      }));
      return;
    }
    if (request.url === "/v1/internal/connectors/execute") {
      response.end(JSON.stringify({ connector_id: body.connector_id, operation: body.operation, result: { repositories: ["wrapper-backend"] } }));
      return;
    }
    if (request.url === "/v1/internal/computers/execute") {
      response.end(JSON.stringify({
        operation: body.operation,
        result: { image_base64: "aW1hZ2U=", mime_type: "image/jpeg", size_bytes: 5 },
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: { message: "not found" } }));
  });

  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    assert.equal(typeof address, "object");
    process.env.PI_CONNECTOR_BROKER_URL = `http://127.0.0.1:${address.port}`;
    const authFile = path.join(temporary, "runtime-auth.json");
    await writeFile(authFile, JSON.stringify({ connector_run_token: "test-run-token" }), { mode: 0o600 });
    process.env.PI_RUNTIME_AUTH_FILE = authFile;

    await build({
      entryPoints: [path.resolve("extensions/connectors/index.ts")],
      outfile: output,
      bundle: true,
      platform: "node",
      format: "esm",
      target: "node22",
    });
    const extension = (await import(`${pathToFileURL(output).href}?v=${Date.now()}`)).default;
    const tools = new Map();
    const handlers = new Map();
    let activeTools = ["read"];
    const pi = {
      registerTool(tool) {
        tools.set(tool.name, tool);
        activeTools.push(tool.name);
      },
      on(name, handler) { handlers.set(name, handler); },
      getActiveTools() { return [...activeTools]; },
      setActiveTools(names) { activeTools = [...names]; },
    };

    extension(pi);
    handlers.get("session_start")();
    assert.deepEqual(activeTools, ["read", "connector_search"]);

    const search = await tools.get("connector_search").execute(
      "search-call", { query: "GitHub repositorios" }, undefined,
    );
    assert.equal(search.isError, undefined);
    assert.deepEqual(activeTools, ["read", "connector_search", "connector_github"]);
    assert.doesNotMatch(activeTools.join(" "), /connector_google_workspace/);

    const calendarSearch = await tools.get("connector_search").execute(
      "calendar-search", { query: "Google Calendar evento" }, undefined,
    );
    assert.equal(calendarSearch.isError, undefined);
    const calendarMatches = JSON.parse(calendarSearch.content[0].text);
    assert.equal(calendarMatches[0].operation_guidance.create_calendar_event.arguments.timezone.includes("IANA"), true);
    assert.match(
      tools.get("connector_google_workspace").parameters.properties.arguments.description,
      /start_datetime ISO 8601 exacto/,
    );

    const result = await tools.get("connector_github").execute(
      "github-call",
      { operation: "search_repositories", arguments: { query: "wrapper" } },
      undefined,
    );
    assert.equal(result.isError, undefined);
    assert.match(result.content[0].text, /wrapper-backend/);
    const computerSearch = await tools.get("connector_search").execute(
      "computer-search", { query: "computadora pantalla" }, undefined,
    );
    assert.equal(computerSearch.isError, undefined);
    assert.match(computerSearch.content[0].text, /Agent Computer/);
    assert.deepEqual(activeTools, [
      "read", "connector_search", "connector_github", "connector_google_workspace", "computer",
    ]);

    const screenshot = await tools.get("computer").execute(
      "computer-call", { operation: "screenshot", arguments: {} }, undefined,
    );
    assert.equal(screenshot.isError, undefined);
    assert.deepEqual(screenshot.content[1], { type: "image", data: "aW1hZ2U=", mimeType: "image/jpeg" });

    assert.equal(requests.length, 5);
    assert.ok(requests.every((request) => request.token === "test-run-token"));
    assert.deepEqual(requests[2].body, {
      connector_id: "github",
      operation: "search_repositories",
      arguments: { query: "wrapper" },
    });
    assert.equal(requests[1].url, "/v1/internal/connectors/catalog");
    assert.equal(requests[3].url, "/v1/internal/connectors/catalog");
    assert.deepEqual(requests[4].body, { operation: "screenshot", arguments: {} });
  } finally {
    delete process.env.PI_CONNECTOR_BROKER_URL;
    delete process.env.PI_CONNECTOR_RUN_TOKEN;
    delete process.env.PI_RUNTIME_AUTH_FILE;
    await new Promise((resolve) => server.close(resolve));
    await rm(temporary, { recursive: true, force: true });
  }
});

test("runtime auth rota el token del modelo sin reiniciar la sesión", async () => {
  const temporary = await mkdtemp(path.join(tmpdir(), "wrapper-runtime-auth-"));
  const output = path.join(temporary, "runtime-auth.mjs");
  const authFile = path.join(temporary, "runtime-auth.json");
  try {
    process.env.PI_RUNTIME_AUTH_FILE = authFile;
    await build({
      entryPoints: [path.resolve("extensions/runtime-auth/index.ts")],
      outfile: output,
      bundle: true,
      platform: "node",
      format: "esm",
      target: "node22",
    });
    const extension = (await import(`${pathToFileURL(output).href}?v=${Date.now()}`)).default;
    const handlers = new Map();
    extension({ on(name, handler) { handlers.set(name, handler); } });
    const apply = async (token) => {
      await writeFile(authFile, JSON.stringify({ run_api_key: token }), { mode: 0o600 });
      const event = { headers: { Authorization: "Bearer stale" } };
      handlers.get("before_provider_headers")(event, {});
      return event.headers.Authorization;
    };
    assert.equal(await apply("run-token-one"), "Bearer run-token-one");
    assert.equal(await apply("run-token-two"), "Bearer run-token-two");
  } finally {
    delete process.env.PI_RUNTIME_AUTH_FILE;
    await rm(temporary, { recursive: true, force: true });
  }
});
