import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";
import { build } from "esbuild";

const root = resolve(import.meta.dirname, "..");
const file = resolve(root, "packages/http/src/generated/tinypods.ts");
const source = ts.createSourceFile(file, await readFile(file, "utf8"), ts.ScriptTarget.Latest, true);
const spec = JSON.parse(await readFile(resolve(root, "openapi.json"), "utf8"));
const declarations = new Map();
for (const statement of source.statements) {
  if (ts.isVariableStatement(statement)) {
    for (const declaration of statement.declarationList.declarations) declarations.set(declaration.name.getText(source), declaration);
  } else if (ts.isFunctionDeclaration(statement) && statement.body) declarations.set(statement.name.text, statement);
}
function calls(node, name) {
  let found = false;
  function visit(child) {
    if (ts.isCallExpression(child) && ts.isIdentifier(child.expression) && child.expression.text === name) found = true;
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
      const hook = declarations.get(`use${name[0].toUpperCase()}${name.slice(1)}`);
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
await build({ entryPoints: [file], bundle: true, platform: "node", format: "esm", outfile: bundle, logLevel: "silent" });
const client = await import(pathToFileURL(bundle).href);
after(async () => { await rm(directory, { recursive: true, force: true }); });

test("generated fetch preserves request options and exposes successful data", async (context) => {
  context.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "/api/v1/info");
    assert.equal(options.method, "GET");
    assert.equal(options.headers["x-request-id"], "fixture");
    return new Response(JSON.stringify({ version: "fixture" }), { status: 200 });
  });
  const result = await client.platformInfoGet({ headers: { "x-request-id": "fixture" } });
  assert.equal(result.status, 200);
  assert.equal(result.data.version, "fixture");
});

test("generated HTTP failures reject for TanStack error state", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({ code: "fixture_error" }), { status: 429 }));
  await assert.rejects(client.platformInfoGet(), (error) => error.status === 429 && error.info.code === "fixture_error");
});

test("generated DELETE accepts an empty 204 response", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  assert.equal((await client.applicationsApplicationDelete("fixture-id")).status, 204);
});
