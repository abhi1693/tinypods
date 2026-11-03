import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { resolve, relative, join } from "node:path";
import { spawnSync } from "node:child_process";

const root = resolve(import.meta.dirname, "..");
const expected = resolve(root, "packages/http/src/generated");
const temporary = await mkdtemp(join(root, ".orval-check-"));
async function files(directory, base = directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) result.push(...(await files(path, base)));
    else result.push([relative(base, path), await readFile(path, "utf8")]);
  }
  return result.sort(([a], [b]) => a.localeCompare(b));
}
try {
  const result = spawnSync(
    process.execPath,
    [
      resolve(root, "node_modules/orval/dist/bin/orval.mjs"),
      "--config",
      "packages/http/orval.config.ts",
    ],
    {
      cwd: root,
      env: { ...process.env, TINYPODS_ORVAL_OUTPUT: temporary },
      stdio: "inherit",
    },
  );
  if (result.status !== 0) throw new Error("Orval generation failed");
  if (
    JSON.stringify(await files(expected)) !==
    JSON.stringify(await files(temporary))
  ) {
    throw new Error(
      "Generated API drift: run npm run api:generate and commit the result",
    );
  }
  console.log("Generated API matches OpenAPI exactly.");
} finally {
  await rm(temporary, { recursive: true, force: true });
}
