import assert from "node:assert/strict";
import { before, after, afterEach, test } from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { resolve, join } from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";
import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";
import { faker } from "@faker-js/faker";
import { MutationObserver, QueryClient } from "@tanstack/react-query";

const root = resolve(import.meta.dirname, "..");
const temporary = await mkdtemp(join(root, ".frontend-test-"));
const bundle = join(temporary, "fixtures.mjs");
await build({
  stdin: {
    contents: [
      'export * from "./packages/http/src/generated/client/endpoints/index.ts";',
      'export * from "./packages/http/src/errors.ts";',
      'export * from "./packages/http/src/generated/client/mocks/authentication/auth-local-register.msw.ts";',
      'export * from "./packages/http/src/generated/client/mocks/setup/setup-owner-create.msw.ts";',
      'export * from "./packages/http/src/generated/client/mocks/setup/setup-status-get.msw.ts";',
      'export * from "./packages/http/src/generated/client/mocks/authentication/auth-local-login.msw.ts";',
      'export * from "./packages/http/src/generated/client/mocks/authentication/auth-session-get.msw.ts";',
      'export * from "./packages/http/src/generated/client/mocks/authentication/auth-providers-list.msw.ts";',
      'export * from "./packages/http/src/generated/client/mocks/authentication/auth-zitadel-login-start.msw.ts";',
    ].join("\n"),
    resolveDir: root,
    loader: "ts",
  },
  outfile: bundle,
  bundle: true,
  packages: "external",
  platform: "node",
  format: "esm",
  logLevel: "silent",
});
const api = await import(pathToFileURL(bundle).href);
const previousOrigin = process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN;
process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN = "http://api.test";
faker.seed(409);
faker.setDefaultRefDate("2030-01-01T00:00:00Z");
const identity = {
  id: "11111111-1111-4111-8111-111111111111",
  username: "fixture",
  is_owner: false,
};
const signedIn = api.getAuthLocalLoginResponseMock({
  user: identity,
  expires_at: "2030-01-02T00:00:00Z",
  refresh_expires_at: null,
});
const credentials = { username: "fixture", password: "fixture-password-value" };
const server = setupServer(
  api.getAuthLocalLoginMockHandler(signedIn),
  api.getAuthSessionGetMockHandler(
    api.getAuthSessionGetResponseMock({ user: identity }),
  ),
);
before(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
after(async () => {
  server.close();
  if (previousOrigin === undefined)
    delete process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN;
  else process.env.NEXT_PUBLIC_TINYPODS_API_ORIGIN = previousOrigin;
  await rm(temporary, { recursive: true, force: true });
});
function client() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

test("generated login/session options work with generated factories and isolated query caches", async () => {
  const query = client();
  const other = client();
  try {
    server.use(
      api.getAuthLocalLoginMockHandler(async ({ request }) => {
        assert.equal(request.url, "http://api.test/api/v1/auth/login");
        assert.equal(request.credentials, "include");
        assert.equal(request.headers.get("x-csrf-token"), "login");
        assert.deepEqual(await request.json(), credentials);
        return signedIn;
      }),
    );
    const mutation = new MutationObserver(
      query,
      api.getAuthLocalLoginMutationOptions({
        mutation: { retry: false, gcTime: 0 },
      }),
    );
    const response = await mutation.mutate({ data: credentials });
    assert.deepEqual(response.data.user, identity);
    query.setQueryData(api.getAuthSessionGetQueryKey(), {
      data: { user: response.data.user },
      status: 200,
      headers: new Headers(),
    });
    assert.equal(
      other.getQueryData(api.getAuthSessionGetQueryKey()),
      undefined,
    );
    const session = await query.fetchQuery(api.getAuthSessionGetQueryOptions());
    assert.deepEqual(session.data.user, identity);
  } finally {
    query.clear();
    other.clear();
  }
});

for (const status of [401, 429]) {
  test(`generated mutation rejects HTTP ${status} without retrying credentials`, async () => {
    let calls = 0;
    server.use(
      http.post(api.getAuthLocalLoginUrl(), () => {
        calls++;
        return HttpResponse.json({}, { status });
      }),
    );
    const query = client();
    try {
      const mutation = new MutationObserver(
        query,
        api.getAuthLocalLoginMutationOptions({
          mutation: { retry: false, gcTime: 0 },
        }),
      );
      await assert.rejects(
        mutation.mutate({ data: credentials }),
        (error) => error.status === status,
      );
      assert.equal(calls, 1);
    } finally {
      query.clear();
    }
  });
}

test("generated mutation exposes pending state and network failures", async () => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  server.use(
    api.getAuthLocalLoginMockHandler(async () => {
      await gate;
      return signedIn;
    }),
  );
  const query = client();
  try {
    const mutation = new MutationObserver(
      query,
      api.getAuthLocalLoginMutationOptions({ mutation: { gcTime: 0 } }),
    );
    const pending = mutation.mutate({ data: credentials });
    assert.equal(mutation.getCurrentResult().isPending, true);
    release();
    await pending;
    server.use(
      http.post(api.getAuthLocalLoginUrl(), () => HttpResponse.error()),
    );
    await assert.rejects(mutation.mutate({ data: credentials }), TypeError);
    assert.equal(mutation.getCurrentResult().isError, true);
  } finally {
    query.clear();
  }
});

test("generated discovery and provider start preserve configured choices and login CSRF", async () => {
  const provider = {
    id: "zitadel",
    label: "Organization",
    type: "redirect",
    login_path: "/api/v1/auth/providers/zitadel/start",
    logout_path: "/api/v1/auth/logout",
    identity_providers: [
      {
        id: "configured-provider",
        label: "Configured social login",
        type: "oauth",
      },
    ],
  };
  server.use(
    api.getAuthProvidersListMockHandler(
      api.getAuthProvidersListResponseMock({ data: [provider] }),
    ),
    api.getAuthZitadelLoginStartMockHandler(async ({ request }) => {
      assert.equal(request.headers.get("x-csrf-token"), "login");
      assert.deepEqual(await request.json(), {
        return_to: "https://console.test/",
        identity_provider_id: "configured-provider",
      });
      return api.getAuthZitadelLoginStartResponseMock({
        authorization_url: "https://issuer.test/authorize",
      });
    }),
  );
  const query = client();
  try {
    const discovery = await query.fetchQuery(
      api.getAuthProvidersListQueryOptions(),
    );
    assert.equal(
      discovery.data.data[0].identity_providers[0].id,
      "configured-provider",
    );
    const mutation = new MutationObserver(
      query,
      api.getAuthZitadelLoginStartMutationOptions({ mutation: { gcTime: 0 } }),
    );
    const response = await mutation.mutate({
      data: {
        return_to: "https://console.test/",
        identity_provider_id: "configured-provider",
      },
    });
    assert.equal(
      response.data.authorization_url,
      "https://issuer.test/authorize",
    );
  } finally {
    query.clear();
  }
});

test("server validation feedback is preserved without client-owned rules", async () => {
  const query = client();
  const info = {
    code: "validation_error",
    message: "Check the highlighted fields.",
    request_id: "fixture",
    issues: [
      {
        location: ["body", "username"],
        code: "source_owned",
        message: "Message supplied by the server.",
      },
      {
        location: ["body", "username"],
        code: "second",
        message: "Second message.",
      },
      {
        location: ["query", "token"],
        code: "ignored",
        message: "Not a form field.",
      },
    ],
  };
  let values;
  server.use(
    http.post(api.getAuthLocalLoginUrl(), async ({ request }) => {
      values = await request.json();
      return HttpResponse.json(info, { status: 422 });
    }),
  );
  try {
    const mutation = new MutationObserver(
      query,
      api.getAuthLocalLoginMutationOptions({
        mutation: { retry: false, gcTime: 0 },
      }),
    );
    await assert.rejects(
      mutation.mutate({ data: { username: "", password: "" } }),
      (error) => {
        assert.deepEqual(api.getValidationFeedback(error), {
          message: info.message,
          fields: { username: "Message supplied by the server." },
        });
        return true;
      },
    );
    assert.deepEqual(values, { username: "", password: "" });
    assert.equal(
      api.getRequestErrorMessage({ info }, "fallback"),
      info.message,
    );
    assert.equal(
      api.getRequestErrorMessage(
        {
          status: 429,
          info: {
            code: "http_429",
            message: "Too Many Requests",
          },
        },
        "fallback",
        { 429: "Wait before retrying." },
      ),
      "Wait before retrying.",
    );
    assert.equal(
      api.getRequestErrorMessage(
        new Error("private transport detail"),
        "Try again.",
      ),
      "Try again.",
    );
    assert.equal(
      api.getRequestErrorMessage(
        {
          status: 503,
          info: {
            code: "setup_conflict",
            message: "Server domain feedback.",
          },
        },
        "fallback",
        { 503: "Unavailable" },
      ),
      "Server domain feedback.",
    );
    assert.equal(
      api.getValidationFeedback({ info: { ...info, code: "other" } }),
      undefined,
    );
    assert.equal(api.getValidationFeedback(null), undefined);
  } finally {
    query.clear();
  }
});

test("generated registration and setup use direct endpoints, CSRF and private token headers", async () => {
  const query = client();
  const data = {
    username: "fixture@example.com",
    password: credentials.password,
    password_confirmation: credentials.password,
  };
  server.use(
    api.getAuthLocalRegisterMockHandler(async ({ request }) => {
      assert.equal(request.credentials, "include");
      assert.equal(request.headers.get("x-csrf-token"), "login");
      assert.deepEqual(await request.json(), data);
      return api.getAuthLocalRegisterResponseMock({
        user: { ...identity, username: data.username },
      });
    }),
    api.getSetupStatusGetMockHandler(async ({ request }) => {
      assert.equal(
        request.headers.get("x-setup-token"),
        "test-only-operator-token",
      );
      return api.getSetupStatusGetResponseMock({ available: true });
    }),
    api.getSetupOwnerCreateMockHandler(async ({ request }) => {
      assert.equal(request.headers.get("x-csrf-token"), "login");
      assert.equal(
        request.headers.get("x-setup-token"),
        "test-only-operator-token",
      );
      const values = await request.json();
      assert.equal(values.email, data.username);
      return api.getSetupOwnerCreateResponseMock({
        user: { ...identity, is_owner: true },
      });
    }),
  );
  try {
    const register = new MutationObserver(
      query,
      api.getAuthLocalRegisterMutationOptions({
        mutation: { retry: false, gcTime: 0 },
      }),
    );
    assert.equal((await register.mutate({ data })).data.user.is_owner, false);
    const headers = { "x-setup-token": "test-only-operator-token" };
    const access = await query.fetchQuery(
      api.getSetupStatusGetQueryOptions({
        fetch: { headers },
        query: { retry: false, gcTime: 0 },
      }),
    );
    assert.equal(access.data.available, true);
    const setup = new MutationObserver(
      query,
      api.getSetupOwnerCreateMutationOptions({
        fetch: { headers },
        mutation: { retry: false, gcTime: 0 },
      }),
    );
    assert.equal(
      (
        await setup.mutate({
          data: {
            email: data.username,
            password: data.password,
            password_confirmation: data.password_confirmation,
          },
        })
      ).data.user.is_owner,
      true,
    );
  } finally {
    query.clear();
  }
});
