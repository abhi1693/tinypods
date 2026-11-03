import { defineConfig } from "orval";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const output = process.env.TINYPODS_ORVAL_OUTPUT ?? resolve(root, "packages/http/src/generated");

export default defineConfig({
  tinypods: {
    input: { target: resolve(root, "openapi.json") },
    output: {
      target: resolve(output, "tinypods.ts"),
      schemas: resolve(output, "models"),
      client: "react-query",
      httpClient: "fetch",
      mode: "split",
      clean: true,
      override: {
        fetch: { forceSuccessResponse: true },
        query: { useSetQueryData: true },
      },
      // Keep Orval's verb defaults: reads are queries, writes are mutations.
    },
  },
});
