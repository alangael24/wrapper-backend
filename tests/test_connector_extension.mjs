import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
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
            operations: ["search_email"],
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
    response.statusCode = 404;
    response.end(JSON.stringify({ error: { message: "not found" } }));
  });

  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    assert.equal(typeof address, "object");
    process.env.PI_CONNECTOR_BROKER_URL = `http://127.0.0.1:${address.port}`;
    process.env.PI_CONNECTOR_RUN_TOKEN = "test-run-token";

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

    const result = await tools.get("connector_github").execute(
      "github-call",
      { operation: "search_repositories", arguments: { query: "wrapper" } },
      undefined,
    );
    assert.equal(result.isError, undefined);
    assert.match(result.content[0].text, /wrapper-backend/);
    assert.equal(requests.length, 2);
    assert.ok(requests.every((request) => request.token === "test-run-token"));
    assert.deepEqual(requests[1].body, {
      connector_id: "github",
      operation: "search_repositories",
      arguments: { query: "wrapper" },
    });
  } finally {
    delete process.env.PI_CONNECTOR_BROKER_URL;
    delete process.env.PI_CONNECTOR_RUN_TOKEN;
    await new Promise((resolve) => server.close(resolve));
    await rm(temporary, { recursive: true, force: true });
  }
});
