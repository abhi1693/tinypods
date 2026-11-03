import { readFileSync } from "node:fs";
import { defineConfig } from "orval";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const output =
  process.env.TINYPODS_ORVAL_OUTPUT ??
  resolve(root, "packages/http/src/generated");
const specPath = resolve(root, "openapi.json");
const spec = JSON.parse(readFileSync(specPath, "utf8"));
const client = resolve(output, "client");
const validation = resolve(output, "validation");

export default defineConfig({
  tinypods: {
    input: { target: specPath },
    output: {
      target: resolve(client, "endpoints/tinypods.ts"),
      schemas: {
        path: resolve(client, "schemas/models"),
        mode: "split",
        type: "typescript",
      },
      operationSchemas: resolve(client, "schemas/types"),
      baseUrl: {
        getBaseUrlFromSpecification: true,
        index: 0,
        variables: {
          origin: {
            runtime: `process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN ?? ${JSON.stringify(spec.servers[0].variables.origin.default)}`,
          },
        },
      },
      client: "react-query",
      httpClient: "fetch",
      mode: "tags-operations",
      indexFiles: true,
      clean: true,
      formatter: "prettier",
      mock: {
        path: resolve(client, "mocks"),
        generators: [{ type: "msw", delay: false, useExamples: true }],
      },
      override: {
        requestOptions: { credentials: "include" },
        fetch: { forceSuccessResponse: true },
        query: { useSetQueryData: true },
        operations: {
          authLocalLogin: {
            requestOptions: {
              credentials: "include",
              headers: { "x-csrf-token": "login" },
            },
          },
          authZitadelLoginStart: {
            requestOptions: {
              credentials: "include",
              headers: { "x-csrf-token": "login" },
            },
          },
        },
      },
    },
  },
  validation: {
    input: { target: specPath },
    output: {
      client: "zod",
      schemas: {
        path: resolve(validation, "models"),
        type: "zod",
        mode: "split",
      },
      clean: true,
      formatter: "prettier",
      override: { zod: { version: 4 } },
    },
  },
});
