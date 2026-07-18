/**
 * Emit each Blueprint fixture as BOTH the source JSON and the YAML this
 * frontend would PUT, so the Python contract test can prove the two parse to an
 * identical canonical digest.
 *
 * Run: npm run fixtures:emit
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { emitBlueprintYaml, pruneEmpty } from "../src/model/emit";
import { FIXTURES } from "../src/model/fixtures";

const here = dirname(fileURLToPath(import.meta.url));
const outDir = resolve(here, "..", "src", "model", "__fixtures__");

mkdirSync(outDir, { recursive: true });

for (const [name, blueprint] of Object.entries(FIXTURES)) {
  writeFileSync(resolve(outDir, `${name}.json`), JSON.stringify(pruneEmpty(blueprint), null, 2) + "\n");
  writeFileSync(resolve(outDir, `${name}.yaml`), emitBlueprintYaml(blueprint));
  console.log(`emitted ${name}.json + ${name}.yaml`);
}
