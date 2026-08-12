import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { readdir, writeFile } from "node:fs/promises";
import path from "node:path";

const roots = process.argv.slice(2);
if (roots.length === 0) {
  console.error("Usage: node scripts/checksums.mjs <artifact-directory> [...]");
  process.exit(2);
}

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await walk(target));
    else if (entry.isFile() && entry.name !== "SHA256SUMS.txt") files.push(target);
  }
  return files;
}

async function sha256(file) {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  return digest.digest("hex");
}

const files = (await Promise.all(roots.map(walk))).flat().sort();
const seen = new Set();
const lines = [];
for (const file of files) {
  const name = path.basename(file);
  if (seen.has(name)) throw new Error(`Duplicate artifact name: ${name}`);
  seen.add(name);
  lines.push(`${await sha256(file)}  ${name}`);
}

if (lines.length === 0) throw new Error("No release artifacts found");
const destination = path.join(roots[0], "SHA256SUMS.txt");
await writeFile(destination, `${lines.join("\n")}\n`, "utf8");
console.log(destination);
