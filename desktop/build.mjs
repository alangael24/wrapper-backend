import { rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

await rm(new URL("./dist", import.meta.url), { recursive: true, force: true });

const localPath = (relativePath) => fileURLToPath(new URL(relativePath, import.meta.url));

const shared = {
  bundle: true,
  sourcemap: true,
  target: "es2022",
  logLevel: "info"
};

await Promise.all([
  build({
    ...shared,
    entryPoints: [localPath("./src/main.ts")],
    outfile: localPath("./dist/main.cjs"),
    platform: "node",
    format: "cjs",
    external: ["electron"]
  }),
  build({
    ...shared,
    entryPoints: [localPath("./src/preload.ts")],
    outfile: localPath("./dist/preload.cjs"),
    platform: "node",
    format: "cjs",
    external: ["electron"]
  }),
  build({
    ...shared,
    entryPoints: [localPath("./src/renderer.ts")],
    outfile: localPath("./dist/renderer.js"),
    platform: "browser",
    format: "iife"
  }),
  build({
    ...shared,
    entryPoints: [localPath("./src/contracts.ts")],
    outfile: localPath("./dist/contracts.cjs"),
    platform: "node",
    format: "cjs"
  })
]);
