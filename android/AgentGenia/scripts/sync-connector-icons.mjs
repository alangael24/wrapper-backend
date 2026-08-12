import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, "../../..");
const drawableDirectory = path.join(repositoryRoot, "android/AgentGenia/app/src/main/res/drawable");
const bitmapDirectory = path.join(repositoryRoot, "android/AgentGenia/app/src/main/res/drawable-nodpi");
const iosAssetDirectory = path.join(repositoryRoot, "ios/AgentGenia/AgentGenia/Assets.xcassets");
const simpleIconsRoot = path.join(repositoryRoot, "node_modules/simple-icons");
const customSource = path.join(repositoryRoot, "desktop/src/connector-logo-data.ts");

const icons = {
  "google-workspace": ["simple", "google"],
  slack: ["simple", "slack"], notion: ["simple", "notion"], salesforce: ["simple", "salesforce"],
  "microsoft-365": ["custom", "microsoft"], linkedin: ["custom", "linkedin"], zoom: ["simple", "zoom"],
  github: ["simple", "github"], jira: ["simple", "jira"], linear: ["simple", "linear"],
  asana: ["simple", "asana"], clickup: ["simple", "clickup"], figma: ["simple", "figma"],
  hubspot: ["simple", "hubspot"], canva: ["simple", "canva"], trello: ["simple", "trello"],
  "monday-com": ["custom", "monday"], intercom: ["simple", "intercom"], zendesk: ["simple", "zendesk"],
  box: ["simple", "box"], dropbox: ["simple", "dropbox"], docusign: ["custom", "docusign"],
  calendly: ["simple", "calendly"], loom: ["simple", "loom"], outreach: ["custom", "outreach"],
  salesloft: ["custom", "salesloft"], apollo: ["custom", "apollo"], clay: ["custom", "clay"],
  zoominfo: ["custom", "zoominfo"], nooks: ["custom", "nooks"], stripe: ["simple", "stripe"],
  quickbooks: ["simple", "quickbooks"], netsuite: ["custom", "netsuite"], ramp: ["custom", "ramp"],
  workday: ["custom", "workday"], rippling: ["custom", "rippling"], ashby: ["custom", "ashby"],
  greenhouse: ["simple", "greenhouse"], vercel: ["simple", "vercel"], tableau: ["custom", "tableau"],
  hex: ["custom", "hex"], amplitude: ["custom", "amplitude"], mixpanel: ["simple", "mixpanel"],
  snowflake: ["simple", "snowflake"], databricks: ["simple", "databricks"], mailchimp: ["simple", "mailchimp"],
  shopify: ["simple", "shopify"], tiendanube: ["custom", "tiendanube"], woocommerce: ["simple", "woocommerce"],
};

const metadata = JSON.parse(fs.readFileSync(path.join(simpleIconsRoot, "data/simple-icons.json"), "utf8"));
const customText = fs.readFileSync(customSource, "utf8");
const custom = new Map();
for (const match of customText.matchAll(/  "([^"]+)": "data:image\/(png|jpeg);base64,([^"]+)"/g)) {
  custom.set(match[1], { extension: match[2] === "jpeg" ? "jpg" : "png", data: Buffer.from(match[3], "base64") });
}

fs.mkdirSync(drawableDirectory, { recursive: true });
fs.mkdirSync(bitmapDirectory, { recursive: true });
fs.mkdirSync(iosAssetDirectory, { recursive: true });
for (const [connectorId, [kind, source]] of Object.entries(icons)) {
  const resourceName = `logo_${connectorId.replaceAll("-", "_")}`;
  const iosImageSet = path.join(iosAssetDirectory, `${resourceName}.imageset`);
  fs.rmSync(iosImageSet, { recursive: true, force: true });
  fs.mkdirSync(iosImageSet, { recursive: true });
  if (kind === "custom") {
    const value = custom.get(source);
    if (!value) throw new Error(`Missing custom icon: ${source}`);
    fs.writeFileSync(path.join(bitmapDirectory, `${resourceName}.${value.extension}`), value.data);
    const images = ["1x", "2x", "3x"].map((scale) => {
      const filename = `${resourceName}@${scale}.${value.extension}`;
      fs.writeFileSync(path.join(iosImageSet, filename), value.data);
      return { filename, idiom: "universal", scale };
    });
    fs.writeFileSync(
      path.join(iosImageSet, "Contents.json"),
      `${JSON.stringify({ images, info: { author: "xcode", version: 1 } }, null, 2)}\n`,
    );
    continue;
  }
  const item = metadata.find((candidate) => candidate.slug === source);
  if (!item) throw new Error(`Missing Simple Icon metadata: ${source}`);
  const svg = fs.readFileSync(path.join(simpleIconsRoot, `icons/${source}.svg`), "utf8");
  const pathData = svg.match(/<path d="([^"]+)"/)?.[1];
  if (!pathData) throw new Error(`Missing path: ${source}`);
  const vector = `<?xml version="1.0" encoding="utf-8"?>\n<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="48dp" android:height="48dp" android:viewportWidth="24" android:viewportHeight="24">\n    <path android:fillColor="#${item.hex}" android:pathData="${pathData}" />\n</vector>\n`;
  fs.writeFileSync(path.join(drawableDirectory, `${resourceName}.xml`), vector);
  const iosFilename = `${resourceName}.svg`;
  const brandedSvg = svg.replace("<svg ", `<svg fill="#${item.hex}" `);
  fs.writeFileSync(path.join(iosImageSet, iosFilename), brandedSvg);
  fs.writeFileSync(
    path.join(iosImageSet, "Contents.json"),
    `${JSON.stringify({
      images: [{ filename: iosFilename, idiom: "universal" }],
      info: { author: "xcode", version: 1 },
      properties: { "preserves-vector-representation": true },
    }, null, 2)}\n`,
  );
}

console.log(`Synchronized ${Object.keys(icons).length} Android and iOS connector icons.`);
