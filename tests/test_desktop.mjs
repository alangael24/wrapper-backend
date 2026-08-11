import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const contracts = require("../desktop/dist/contracts.cjs");

test("ships the complete connector catalog from work and commerce apps", () => {
  const ids = contracts.CONNECTOR_CATALOG.map((item) => item.id);
  assert.equal(ids.length, 18);
  for (const id of [
    "google-workspace", "slack", "notion", "salesforce", "microsoft-365",
    "linkedin", "zoom", "github", "jira", "figma", "hubspot", "canva",
    "linear", "asana", "clickup", "shopify", "tiendanube", "woocommerce"
  ]) assert.ok(ids.includes(id), `missing connector ${id}`);
});

test("normalizes connector selection and persisted bot state", () => {
  const normalized = contracts.normalizeConnectorIds([
    "slack", "slack", "not-real", "shopify", 42
  ]);
  assert.deepEqual(normalized, ["slack", "shopify"]);

  const state = contracts.normalizeAppState({
    version: 99,
    onboardingCompleted: true,
    selectedConnectorIds: ["github", "not-real"],
    activeBotId: "bot-1",
    bots: [{
      id: "bot-1",
      name: "  Mi   bot  ",
      color: "#2f91f5",
      shape: "circle",
      connectorIds: ["github", "not-real"],
      createdAt: "2026-08-11T00:00:00.000Z"
    }]
  });
  assert.equal(state.version, 1);
  assert.deepEqual(state.selectedConnectorIds, ["github"]);
  assert.equal(state.bots[0].name, "Mi bot");
  assert.deepEqual(state.bots[0].connectorIds, ["github"]);
  assert.equal(state.bots[0].setup.step, "purpose");
  assert.equal(state.activeBotId, "bot-1");
});

test("walks a new bot through work setup and connector recommendations", () => {
  let bot = contracts.createBotProfile({
    name: "Juan",
    color: "#2f91f5",
    shape: "drop"
  }, [], "bot-setup", new Date("2026-08-11T01:02:03.000Z"));
  assert.equal(bot.setup.step, "purpose");

  bot = contracts.applyBotSetupAnswer(bot, { step: "purpose", value: "work" });
  assert.equal(bot.setup.step, "workspace");
  bot = contracts.applyBotSetupAnswer(bot, { step: "workspace", value: "mix" });
  assert.equal(bot.setup.step, "project");
  assert.deepEqual(bot.connectorIds, ["google-workspace", "slack"]);
  bot = contracts.applyBotSetupAnswer(bot, { step: "project", value: "notion" });
  assert.equal(bot.setup.step, "connections");
  assert.deepEqual(bot.connectorIds, ["google-workspace", "slack", "notion"]);
  bot = contracts.applyBotSetupAnswer(bot, { step: "connections", value: "complete" });
  assert.equal(bot.setup.step, "complete");
  assert.throws(() => contracts.applyBotSetupAnswer(bot, { step: "purpose", value: "personal" }));
});

test("creates bots with bounded validated fields", () => {
  const bot = contracts.createBotProfile({
    name: "   Turno    nocturno   ",
    color: "not-a-color",
    shape: "not-a-shape"
  }, ["slack", "slack", "fake"], "bot-2", new Date("2026-08-11T01:02:03.000Z"));
  assert.equal(bot.name, "Turno nocturno");
  assert.equal(bot.color, contracts.BOT_COLORS[6]);
  assert.equal(bot.shape, contracts.BOT_SHAPES[0]);
  assert.deepEqual(bot.connectorIds, ["slack"]);
  assert.throws(() => contracts.createBotProfile({ name: "", color: "", shape: "" }, [], "bot-3"));
});

test("keeps Electron renderer isolated from Node and external network", async () => {
  const [main, preload, html] = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/renderer/index.html", import.meta.url), "utf8")
  ]);
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /nodeIntegration:\s*false/);
  assert.match(main, /sandbox:\s*true/);
  assert.match(preload, /contextBridge\.exposeInMainWorld\("wrapperDesktop"/);
  assert.match(html, /connect-src 'none'/);
});

test("desktop layer does not import or rewrite the Pi harness", async () => {
  const files = await Promise.all([
    readFile(new URL("../desktop/src/main.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/preload.ts", import.meta.url), "utf8"),
    readFile(new URL("../desktop/src/renderer.ts", import.meta.url), "utf8")
  ]);
  for (const source of files) assert.doesNotMatch(source, /pi_harness|go_backend/);
});
