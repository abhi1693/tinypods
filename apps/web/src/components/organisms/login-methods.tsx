"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import {
  getAuthZitadelLoginStartUrl,
  useAuthProvidersList,
  useAuthZitadelLoginStart,
} from "@tinypods/http/client";
import { getRequestErrorMessage } from "@tinypods/http/errors";
import type { LoginResponse } from "@tinypods/http/models";
import { RequestFeedback } from "@tinypods/ui/components/molecules/request-feedback";
import { ProviderButtons } from "@tinypods/ui/components/molecules/provider-buttons";
import { LoginForm } from "./login-form";
import { RegistrationForm } from "./registration-form";

export function LoginMethods({
  onLogin,
  onRegistered,
  mode = "login",
}: {
  onLogin: (identity: LoginResponse) => void;
  onRegistered: () => void;
  mode?: "login" | "register";
}) {
  const starting = useRef(false);
  const [active, setActive] = useState<string | null>(null);
  const [startFailure, setStartFailure] = useState<string>();
  const providers = useAuthProvidersList({
    query: { retry: false },
    fetch: { cache: "no-store" },
  });
  const redirect = useAuthZitadelLoginStart({
    mutation: { retry: false, gcTime: 0 },
  });
  if (providers.isPending && !providers.isFetched)
    return <RequestFeedback pending message="Loading sign-in methods…" />;
  if (!providers.data)
    return (
      <RequestFeedback
        message={getRequestErrorMessage(
          providers.error,
          "Cannot load sign-in methods.",
        )}
        pending={providers.isFetching}
        onRetry={() => {
          void providers.refetch();
        }}
      />
    );
  const allowed = providers.data.data.registration_allowed;
  const methods = providers.data.data.data.filter(
    (provider) => mode === "login" || !!provider.registration_path,
  );
  if (mode === "register" && !allowed)
    return (
      <div className="mt-6 text-sm">
        <p>Registration is currently closed.</p>
        <Link className="mt-4 block underline" href="/auth/login">
          Back to sign in
        </Link>
      </div>
    );
  const options = methods
    .filter((provider) => provider.type === "redirect")
    .filter(
      (provider) =>
        provider.login_path === new URL(getAuthZitadelLoginStartUrl()).pathname,
    )
    .flatMap((provider) => [
      {
        id: provider.id,
        label:
          mode === "register" ? "Continue to registration" : provider.label,
        iconUrl: provider.icon_url,
        iconLabel: provider.label,
        identity: undefined as string | undefined,
      },
      ...(provider.identity_providers ?? []).map((identity) => ({
        id: provider.id + ":" + identity.id,
        label:
          (mode === "register" ? "Create account with " : "Continue with ") +
          identity.label,
        iconUrl: identity.icon_url,
        iconLabel: identity.label,
        identity: identity.id,
      })),
    ]);
  async function start(id: string) {
    if (starting.current || active !== null) return;
    const option = options.find((candidate) => candidate.id === id);
    if (!option) return;
    starting.current = true;
    setActive(id);
    setStartFailure(undefined);
    try {
      const response = await redirect.mutateAsync({
        data: {
          return_to: window.location.origin + "/",
          action: mode,
          identity_provider_id: option.identity,
        },
      });
      window.location.assign(response.data.authorization_url);
    } catch (error) {
      setStartFailure(
        getRequestErrorMessage(
          error,
          "Cannot start sign-in. Try again; if it continues, contact your administrator.",
          {
            429: "Too many sign-in attempts. Wait a minute and try again.",
            403: "Sign-in could not be verified. Reload and try again.",
            404: "This sign-in method is no longer available. Reload to see current options.",
          },
        ),
      );
      setActive(null);
      starting.current = false;
    }
  }
  function localPending(providerId: string, pending: boolean) {
    setActive(pending ? "local:" + providerId : null);
  }
  return (
    <>
      {methods
        .filter((provider) => provider.type === "password")
        .map((provider) => (
          <section key={provider.id} aria-label={provider.label}>
            {mode === "register" ? (
              <RegistrationForm
                onRegistered={onRegistered}
                disabled={active !== null && active !== "local:" + provider.id}
                onPendingChange={(pending) =>
                  localPending(provider.id, pending)
                }
              />
            ) : (
              <LoginForm
                onLogin={onLogin}
                disabled={active !== null && active !== "local:" + provider.id}
                onPendingChange={(pending) =>
                  localPending(provider.id, pending)
                }
              />
            )}
          </section>
        ))}
      {options.length > 0 &&
        methods.some((provider) => provider.type === "password") && (
          <div className="mt-6 flex items-center gap-3 text-xs text-muted-foreground">
            <span className="h-px flex-1 bg-border" />
            <span>or</span>
            <span className="h-px flex-1 bg-border" />
          </div>
        )}
      <ProviderButtons
        options={options}
        pendingId={active}
        disabled={active !== null}
        onSelect={(id) => void start(id)}
      />
      {startFailure && (
        <p className="mt-3 text-sm text-red-700" role="alert">
          {startFailure}
        </p>
      )}
      {methods.length === 0 && (
        <p>No sign-in methods are configured. Contact your administrator.</p>
      )}
      <p className="mt-6 text-center text-sm text-muted-foreground">
        {mode === "register" ? (
          <Link className="underline underline-offset-4" href="/auth/login">
            Already have an account? Sign in
          </Link>
        ) : (
          allowed &&
          methods.some((provider) => provider.registration_path) && (
            <Link
              className="underline underline-offset-4"
              href="/auth/register"
            >
              Create an account
            </Link>
          )
        )}
      </p>
    </>
  );
}
