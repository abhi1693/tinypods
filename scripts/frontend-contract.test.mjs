import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readFile, readdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
import { build } from "esbuild";

const root = resolve(import.meta.dirname, "..");
const api = resolve(root, "packages/http/src/generated/client/endpoints");
const file = resolve(api, "index.ts");
async function operationFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map((entry) =>
      entry.isDirectory()
        ? operationFiles(resolve(directory, entry.name))
        : entry.name.endsWith(".ts") && entry.name !== "index.ts"
          ? [resolve(directory, entry.name)]
          : [],
    ),
  );
  return nested.flat().sort();
}
const source = ts.createSourceFile(
  file,
  (
    await Promise.all(
      (await operationFiles(api)).map((path) => readFile(path, "utf8")),
    )
  ).join("\n"),
  ts.ScriptTarget.Latest,
  true,
);
const spec = JSON.parse(await readFile(resolve(root, "openapi.json"), "utf8"));
const declarations = new Map();
for (const statement of source.statements) {
  if (ts.isVariableStatement(statement)) {
    for (const declaration of statement.declarationList.declarations)
      declarations.set(declaration.name.getText(source), declaration);
  } else if (ts.isFunctionDeclaration(statement) && statement.body)
    declarations.set(statement.name.text, statement);
}
function calls(node, name) {
  let found = false;
  function visit(child) {
    if (
      ts.isCallExpression(child) &&
      ts.isIdentifier(child.expression) &&
      child.expression.text === name
    )
      found = true;
    ts.forEachChild(child, visit);
  }
  visit(node);
  return found;
}
for (const [path, operations] of Object.entries(spec.paths)) {
  for (const [method, operation] of Object.entries(operations)) {
    if (!["get", "post", "put", "patch", "delete"].includes(method)) continue;
    const name = operation.operationId;
    test(`${method.toUpperCase()} ${path} generates a request and ${method === "get" ? "query" : "mutation"}`, () => {
      assert.ok(declarations.has(name), `Missing request function: ${name}`);
      const hook = declarations.get(
        `use${name[0].toUpperCase()}${name.slice(1)}`,
      );
      assert.ok(hook, `Missing hook: ${name}`);
      assert.ok(calls(hook, method === "get" ? "useQuery" : "useMutation"));
      const title = `${name[0].toUpperCase()}${name.slice(1)}`;
      if (method === "get") {
        assert.ok(declarations.has(`get${title}QueryKey`));
        assert.ok(declarations.has(`get${title}QueryOptions`));
        assert.ok(declarations.has(`useSet${title}QueryData`));
      } else {
        assert.ok(declarations.has(`get${title}MutationOptions`));
      }
    });
  }
}
// Exercise generator-owned request behavior with a local fetch stub, not a backend.
const directory = await mkdtemp(join(tmpdir(), "tinypods-client-test-"));
const bundle = join(directory, "client.mjs");
await build({
  entryPoints: [file],
  bundle: true,
  platform: "node",
  format: "esm",
  outfile: bundle,
  logLevel: "silent",
});
const client = await import(pathToFileURL(bundle).href);
after(async () => {
  await rm(directory, { recursive: true, force: true });
});

test("generated fetch preserves request options and exposes successful data", async (context) => {
  context.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(
      url,
      `${process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN ?? spec.servers[0].variables.origin.default}/api/v1/info`,
    );
    assert.equal(options.method, "GET");
    assert.equal(options.headers["x-request-id"], "fixture");
    return new Response(JSON.stringify({ version: "fixture" }), {
      status: 200,
    });
  });
  const result = await client.platformInfoGet({
    headers: { "x-request-id": "fixture" },
  });
  assert.equal(result.status, 200);
  assert.equal(result.data.version, "fixture");
});

test("generated HTTP failures reject for TanStack error state", async (context) => {
  context.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(JSON.stringify({ code: "fixture_error" }), { status: 429 }),
  );
  await assert.rejects(
    client.platformInfoGet(),
    (error) => error.status === 429 && error.info.code === "fixture_error",
  );
});

test("generated DELETE accepts an empty 204 response", async (context) => {
  context.mock.method(
    globalThis,
    "fetch",
    async () => new Response(null, { status: 204 }),
  );
  assert.equal(
    (await client.applicationsApplicationDelete("fixture-id")).status,
    204,
  );
});

test("generated login calls the configured API directly with credentials and CSRF", async (context) => {
  const previous = process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN;
  context.after(() => {
    if (previous === undefined)
      delete process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN;
    else process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN = previous;
  });
  process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN = "https://api.example.test";
  context.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "https://api.example.test/api/v1/auth/login");
    assert.equal(options.credentials, "include");
    assert.equal(options.headers["x-csrf-token"], "login");
    assert.equal(options.method, "POST");
    assert.deepEqual(JSON.parse(options.body), {
      username: "fixture",
      password: "fixture-password-value",
    });
    return Response.json({ user: { username: "fixture" } });
  });
  await client.authLocalLogin({
    username: "fixture",
    password: "fixture-password-value",
  });
});

test("generated Zod login schema preserves OpenAPI request bounds", async () => {
  const validation = join(directory, "validation.mjs");
  await build({
    entryPoints: [
      resolve(
        root,
        "packages/http/src/generated/validation/models/loginRequest.zod.ts",
      ),
    ],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: validation,
    logLevel: "silent",
  });
  const { LoginRequest } = await import(pathToFileURL(validation).href);
  const fields = spec.components.schemas.LoginRequest.properties;
  const valid = {
    username: "owner.test-1",
    password: "p".repeat(fields.password.minLength),
  };
  assert.equal(LoginRequest.safeParse(valid).success, true);
  for (const invalid of [
    { ...valid, username: "" },
    { ...valid, username: "spaces forbidden" },
    { ...valid, username: "u".repeat(fields.username.maxLength + 1) },
    { ...valid, password: "p".repeat(fields.password.minLength - 1) },
    { ...valid, password: "p".repeat(fields.password.maxLength + 1) },
  ])
    assert.equal(LoginRequest.safeParse(invalid).success, false);
});
