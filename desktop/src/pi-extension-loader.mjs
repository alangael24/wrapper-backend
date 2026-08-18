// Keep third-party Pi extension source out of the application's TypeScript
// program while still letting esbuild bundle it into the signed Electron main
// process. Both packages publish TypeScript extension entrypoints by design.
export { default as piChromeExtension } from "pi-chrome/extensions/chrome-profile-bridge/index.ts";
export { default as computerUseExtension } from "@injaneity/pi-computer-use/extensions/computer-use.ts";
export { default as connectorExtension } from "../../extensions/connectors/index.ts";
