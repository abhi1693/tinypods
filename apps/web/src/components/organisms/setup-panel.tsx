"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useSetupOwnerCreate, useSetupStatusGet } from "@tinypods/http/client";
import {
  getRequestErrorMessage,
  getValidationFeedback,
} from "@tinypods/http/errors";
import { RequestFeedback } from "@tinypods/ui/components/molecules/request-feedback";
import { LoginForm } from "@tinypods/ui/components/organisms/login-form";
import { AuthHeading } from "../molecules/auth-heading";

const setupStatusMessages = {
  409: "Setup is already complete.",
  403: "This setup link is invalid or no longer available.",
  404: "This setup link is invalid or no longer available.",
  429: "Too many setup attempts. Wait a minute and try again.",
};

/** Capture once in memory and scrub the operator token before any setup requests. */
export function SetupPanel() {
  const captured = useRef(false);
  const [token, setToken] = useState<string | null>();
  const [complete, setComplete] = useState(false);
  useEffect(() => {
    if (captured.current) return;
    captured.current = true;
    const url = new URL(window.location.href);
    const value = url.searchParams.get("token");
    url.searchParams.delete("token");
    window.history.replaceState(
      window.history.state,
      "",
      url.pathname + url.search + url.hash,
    );
    setToken(value);
  }, []);
  const headers = token ? { "x-setup-token": token } : undefined;
  const access = useSetupStatusGet({
    query: { enabled: !!token, retry: false, gcTime: 0 },
    fetch: { headers, cache: "no-store" },
  });
  const setup = useSetupOwnerCreate({
    mutation: { retry: false, gcTime: 0 },
    fetch: { headers },
  });
  const feedback = getValidationFeedback(setup.error);
  const setupError = setup.isError
    ? getRequestErrorMessage(
        setup.error,
        "Cannot finish setup. Check your connection and try again.",
        setupStatusMessages,
      )
    : undefined;
  if (complete)
    return (
      <div role="status" className="text-center">
        <h1 className="text-lg font-semibold">Setup complete</h1>
        <p className="mt-4 text-sm">Your account is ready.</p>
        <Link className="mt-6 block text-sm underline" href="/auth/login">
          Sign in
        </Link>
      </div>
    );
  if (token === undefined || (token && access.isPending && !access.isFetched))
    return <RequestFeedback pending message="Checking setup access…" />;
  if (!token)
    return (
      <RequestFeedback message="Use the private setup link provided by your administrator." />
    );
  if (!access.data?.data.available) {
    const status = access.error?.status;
    const unavailable = status === 403 || status === 404 || status === 409;
    return (
      <RequestFeedback
        pending={access.isFetching}
        message={getRequestErrorMessage(
          access.error,
          "Cannot check setup access. Check your connection and try again.",
          setupStatusMessages,
        )}
        onRetry={
          unavailable
            ? undefined
            : () => {
                void access.refetch();
              }
        }
      />
    );
  }
  return (
    <>
      <AuthHeading mode="setup" />
      <p className="mt-4 text-center text-sm text-muted-foreground">
        Create the account that will manage this installation.
      </p>
      <LoginForm
        mode="setup"
        pending={setup.isPending}
        fieldErrors={feedback?.fields}
        error={setupError}
        onSubmit={async (values) => {
          setup.reset();
          await setup.mutateAsync({
            data: {
              email: values.email ?? "",
              password: values.password,
              password_confirmation: values.password_confirmation ?? "",
            },
          });
          setup.reset();
          setToken(null);
          setComplete(true);
        }}
      />
    </>
  );
}
