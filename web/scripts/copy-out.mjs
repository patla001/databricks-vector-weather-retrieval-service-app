/**
 * Move the static export to ../static, which is what Flask serves.
 *
 * The build output is committed rather than built on deploy: a Databricks App
 * runs a Python runtime with no Node, so there is nowhere to run `next build`
 * on the far side of `databricks sync`. Committing the export is the tradeoff
 * that keeps the app one deployable artifact.
 */
import { cpSync, rmSync, existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const OUT = "out";
const DEST = join("..", "static");

if (!existsSync(OUT)) {
  console.error("No out/ directory - did `next build` run?");
  process.exit(1);
}

rmSync(DEST, { recursive: true, force: true });
cpSync(OUT, DEST, { recursive: true });

let files = 0;
let bytes = 0;
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const info = statSync(path);
    if (info.isDirectory()) walk(path);
    else {
      files += 1;
      bytes += info.size;
    }
  }
};
walk(DEST);
console.log(`copied ${files} files (${(bytes / 1024 / 1024).toFixed(2)} MB) -> static/`);
