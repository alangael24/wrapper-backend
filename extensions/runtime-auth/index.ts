import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const AUTH_FILE_ENV = "PI_RUNTIME_AUTH_FILE";

interface RuntimeAuth {
  run_api_key?: unknown;
}

function currentRunToken(): string {
  const authFile = process.env[AUTH_FILE_ENV] ?? "";
  if (!authFile) throw new Error("La sesión de Pi no tiene credenciales de ejecución.");
  let parsed: RuntimeAuth;
  try {
    parsed = JSON.parse(readFileSync(authFile, "utf8")) as RuntimeAuth;
  } catch {
    throw new Error("No se pudo leer la credencial efímera de esta ejecución.");
  }
  if (typeof parsed.run_api_key !== "string" || !parsed.run_api_key) {
    throw new Error("La credencial efímera de esta ejecución ya no está activa.");
  }
  return parsed.run_api_key;
}

export default function runtimeAuthExtension(pi: ExtensionAPI): void {
  pi.on("before_provider_headers", (event) => {
    for (const name of Object.keys(event.headers)) {
      if (name.toLowerCase() === "authorization") event.headers[name] = null;
    }
    event.headers.Authorization = `Bearer ${currentRunToken()}`;
  });
}
