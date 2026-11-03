"use client";

import Link from "next/link";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getAuthSessionGetQueryKey,
  useAuthSessionGet,
} from "@tinypods/http/client";
import { getRequestErrorMessage } from "@tinypods/http/errors";
import { RequestFeedback } from "@tinypods/ui/components/molecules/request-feedback";
import { AuthHeading } from "../molecules/auth-heading";
import { LoginMethods } from "@tinypods/web-auth/login-methods";

export function LoginPanel({
  mode = "login",
}: {
  mode?: "login" | "register";
}) {
  const [created, setCreated] = useState(false);
  const client = useQueryClient();
  const session = useAuthSessionGet({
    query: { retry: false, gcTime: 0 },
    fetch: { cache: "no-store" },
  });
  if (created)
    return (
      <div role="status" className="text-center">
        <h1 className="text-lg font-semibold">Account created</h1>
        <p className="mt-4 text-sm">Your account is ready to use.</p>
        <Link className="mt-6 block text-sm underline" href="/auth/login">
          Sign in
        </Link>
      </div>
    );
  if (session.isPending && !session.isFetched)
    return <RequestFeedback pending message="Checking your session…" />;
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
      <RequestFeedback
        message={getRequestErrorMessage(
          session.error,
          "Cannot check your session. Check your connection and try again.",
        )}
        pending={session.isFetching}
        onRetry={() => {
          void session.refetch();
        }}
      />
    );
  }
  return (
    <>
      <AuthHeading mode={mode} />
      <LoginMethods
        mode={mode}
        onRegistered={() => setCreated(true)}
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
