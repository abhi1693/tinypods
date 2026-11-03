"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  getAuthSessionGetQueryKey,
  useAuthSessionGet,
} from "@tinypods/http/client";
import { LoginMethods } from "./login-methods";

export function LoginPanel() {
  const client = useQueryClient();
  const session = useAuthSessionGet({
    query: { retry: false, gcTime: 0 },
    fetch: { cache: "no-store" },
  });
  if (session.isPending) return <p role="status">Checking your session…</p>;
  if (session.data && !session.error) {
    return (
      <div role="status">
        <h1 className="text-center text-lg font-semibold tracking-tight">
          Signed in
        </h1>
        <p className="mt-4">Signed in as {session.data.data.user.username}.</p>
      </div>
    );
  }
  if (session.error?.status !== 401) {
    return (
      <div>
        <p role="alert">
          Cannot check your session. Check your connection and try again.
        </p>
        <button
          className="mt-4 rounded border px-4 py-2"
          onClick={() => session.refetch()}
        >
          Try again
        </button>
      </div>
    );
  }
  return (
    <>
      <h1 className="text-center text-lg font-semibold tracking-tight">
        Sign in to TinyPods
      </h1>
      <LoginMethods
        onLogin={(identity) => {
          client.setQueryData(getAuthSessionGetQueryKey(), {
            data: { user: identity.user },
            status: 200,
            headers: new Headers(),
          });
        }}
      />
    </>
  );
}
