import { readFile, rm } from "node:fs/promises";
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

// Both Pi packages publish TypeScript extension entrypoints. Adapt only their
// asset-root lookup for an ASAR-packaged Electron process; tool behavior stays
// native/upstream and the real assets live in extraResources.
const packagedPiAssetRoots = {
  name: "packaged-pi-asset-roots",
  setup(build) {
    build.onLoad({ filter: /pi-chrome\/extensions\/chrome-profile-bridge\/index\.ts$/ }, async (args) => {
      const source = await readFile(args.path, "utf8");
      const needle = "function extensionRoot(): string {";
      if (!source.includes(needle)) throw new Error("pi-chrome extensionRoot contract changed");
      return {
        loader: "ts",
        contents: source
          .replace(
            "const PI_CHROME_PKG_PATH = resolve(__dirname, \"..\", \"..\", \"package.json\");",
            "const PI_CHROME_PKG_PATH = resolve(process.env.PI_CHROME_EXTENSION_ROOT || process.cwd(), \"..\", \"..\", \"package.json\");"
          )
          .replace(
            needle,
            `${needle}\n\tconst packagedRoot = process.env.PI_CHROME_EXTENSION_ROOT;\n\tif (packagedRoot) return packagedRoot;`
          )
      };
    });
    build.onLoad({ filter: /pi-computer-use\/src\/platform\/(?:macos|windows|linux)\/helper\.ts$/ }, async (args) => {
      const source = await readFile(args.path, "utf8");
      const rootExpression = /path\.resolve\(path\.dirname\(fileURLToPath\(import\.meta\.url\)\),\s*"\.\.",\s*"\.\.",\s*"\.\."\)/;
      if (!rootExpression.test(source)) throw new Error(`pi-computer-use PACKAGE_ROOT contract changed: ${args.path}`);
      return {
        loader: "ts",
        contents: source.replace(
          rootExpression,
          `(process.env.PI_COMPUTER_USE_PACKAGE_ROOT || path.join(process.resourcesPath, "pi-runtime", "pi-computer-use"))`
        )
      };
    });
  }
};

await Promise.all([
  build({
    ...shared,
    entryPoints: [localPath("./src/main.ts")],
    outfile: localPath("./dist/main.mjs"),
    platform: "node",
    format: "esm",
    banner: {
      js: "import { createRequire as __agentgeniaCreateRequire } from 'node:module'; const require = __agentgeniaCreateRequire(import.meta.url);"
    },
    external: ["electron"],
    plugins: [packagedPiAssetRoots]
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
