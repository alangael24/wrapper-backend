import { rm } from "node:fs/promises";
import { build } from "esbuild";

await rm(new URL("./dist", import.meta.url), { recursive: true, force: true });

const shared = {
  bundle: true,
  sourcemap: true,
  target: "es2022",
  logLevel: "info"
};

await Promise.all([
  build({
    ...shared,
    entryPoints: [new URL("./src/main.ts", import.meta.url).pathname],
    outfile: new URL("./dist/main.cjs", import.meta.url).pathname,
    platform: "node",
    format: "cjs",
    external: ["electron"]
  }),
  build({
    ...shared,
    entryPoints: [new URL("./src/preload.ts", import.meta.url).pathname],
    outfile: new URL("./dist/preload.cjs", import.meta.url).pathname,
    platform: "node",
    format: "cjs",
    external: ["electron"]
  }),
  build({
    ...shared,
    entryPoints: [new URL("./src/renderer.ts", import.meta.url).pathname],
    outfile: new URL("./dist/renderer.js", import.meta.url).pathname,
    platform: "browser",
    format: "iife"
  }),
  build({
    ...shared,
    entryPoints: [new URL("./src/contracts.ts", import.meta.url).pathname],
    outfile: new URL("./dist/contracts.cjs", import.meta.url).pathname,
    platform: "node",
    format: "cjs"
  })
]);
