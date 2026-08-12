import { access, readFile } from "node:fs/promises";

const requiredFiles = [
  "LICENSE", "SECURITY.md", "PRIVACY.md", "TERMS.md", "EULA.md", "CHANGELOG.md",
  "Dockerfile", ".dockerignore", "render.yaml", "electron-builder.yml",
  ".github/workflows/backend.yml", ".github/workflows/release-config.yml",
  ".github/workflows/desktop.yml",
  ".github/workflows/desktop-release.yml", ".github/workflows/desktop-rollback.yml",
  ".github/workflows/ios.yml",
  ".github/workflows/ios-release.yml", ".github/workflows/android.yml",
  ".github/workflows/android-release.yml",
  "docs/distribution.md", "docs/store-submission.md",
  "ios/AgentGenia/AgentGenia.xcodeproj/xcshareddata/xcschemes/AgentGenia.xcscheme",
  "ios/AgentGenia/AgentGenia/PrivacyInfo.xcprivacy",
  "ios/AgentGenia/AgentGeniaTests/ModelsTests.swift",
  "ios/AgentGenia/AgentGeniaUITests/AgentGeniaUITests.swift",
  "android/AgentGenia/app/src/androidTest/java/com/agentgenia/android/SecureStoreInstrumentedTest.kt",
  "android/AgentGenia/app/src/androidTest/java/com/agentgenia/android/AppLaunchInstrumentedTest.kt",
  "desktop/resources/appx/StoreLogo.png",
  "desktop/resources/appx/Square150x150Logo.png",
  "desktop/resources/appx/Square44x44Logo.png",
  "desktop/resources/appx/Wide310x150Logo.png",
  "desktop/resources/eula.txt",
];
await Promise.all(requiredFiles.map((file) => access(file)));

const packageJson = JSON.parse(await readFile("package.json", "utf8"));
const project = await readFile("ios/AgentGenia/AgentGenia.xcodeproj/project.pbxproj", "utf8");
const gradle = await readFile("android/AgentGenia/app/build.gradle.kts", "utf8");
const builder = await readFile("electron-builder.yml", "utf8");
const workflowFiles = requiredFiles.filter((file) => file.startsWith(".github/workflows/"));

const iosVersion = project.match(/MARKETING_VERSION = ([^;]+);/)?.[1];
const androidVersion = gradle.match(/versionName = "([^"]+)"/)?.[1];
if (!iosVersion || !androidVersion) throw new Error("Unable to read mobile versions");
if (packageJson.version !== iosVersion || packageJson.version !== androidVersion) {
  throw new Error(`Version mismatch: desktop=${packageJson.version}, ios=${iosVersion}, android=${androidVersion}`);
}
if (!/^\d+\.\d+\.\d+$/.test(packageJson.devDependencies?.electron ?? "")) {
  throw new Error("Electron must be pinned to an exact stable version");
}
if (!project.includes("AgentGeniaTests") || !project.includes("AgentGeniaUITests")) {
  throw new Error("iOS test targets are missing");
}
if (!gradle.includes("signingConfig = signingConfigs.getByName(\"release\")")) {
  throw new Error("Android release signing is not fail-closed");
}
if (!gradle.includes("compileSdk = 36") || !gradle.includes("targetSdk = 36")) {
  throw new Error("Android releases must use the stable API 36 SDK");
}
for (const token of ["target: dmg", "target: pkg", "target: nsis", "target: appx", "AppImage", "deb", "rpm"]) {
  if (!builder.includes(token)) throw new Error(`Missing Electron target: ${token}`);
}
for (const file of workflowFiles) {
  const workflow = await readFile(file, "utf8");
  if (/uses:\s+[^\s]+@v\d+/u.test(workflow)) {
    throw new Error(`${file} contains a mutable action version instead of a commit SHA`);
  }
}
console.log(`Release configuration verified for Agent Genia ${packageJson.version}.`);
