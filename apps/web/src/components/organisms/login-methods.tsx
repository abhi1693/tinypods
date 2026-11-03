"use client";

import { useRef } from "react";
import {
  getAuthZitadelLoginStartUrl,
  useAuthProvidersList,
  useAuthZitadelLoginStart,
} from "@tinypods/http/client";
import type { LoginResponse } from "@tinypods/http/models";
import { Button } from "@tinypods/ui/components/atoms/button";
import { ProviderButtons } from "@tinypods/ui/components/molecules/provider-buttons";
import { LoginForm } from "./login-form";

export function LoginMethods({
  onLogin,
}: {
  onLogin: (identity: LoginResponse) => void;
}) {
  const starting = useRef(false);
  const providers = useAuthProvidersList({
    query: { retry: false },
    fetch: { cache: "no-store" },
  });
  const redirect = useAuthZitadelLoginStart({
    mutation: { retry: false, gcTime: 0 },
  });
  if (providers.isPending) return <p role="status">Loading sign-in methods…</p>;
  if (!providers.data)
    return (
      <div>
        <p role="alert">Cannot load sign-in methods.</p>
        <Button onClick={() => providers.refetch()}>Try again</Button>
      </div>
    );
  const methods = providers.data.data.data;
  const options = methods
    .filter((provider) => provider.type === "redirect")
    .filter(
      (provider) =>
        provider.login_path === new URL(getAuthZitadelLoginStartUrl()).pathname,
    )
    .flatMap((provider) => [
      {
        id: provider.id,
        label: provider.label,
        identity: undefined as string | undefined,
      },
      ...(provider.identity_providers ?? []).map((identity) => ({
        id: provider.id + ":" + identity.id,
        label: identity.label,
        identity: identity.id,
      })),
    ]);
  async function start(id: string) {
    if (starting.current) return;
    const option = options.find((candidate) => candidate.id === id);
    if (!option) return;
    starting.current = true;
    try {
      const response = await redirect.mutateAsync({
        data: {
          return_to: window.location.origin + "/",
          identity_provider_id: option.identity,
        },
      });
      window.location.assign(response.data.authorization_url);
    } catch {
      starting.current = false;
    }
  }
  return (
    <>
      {methods
        .filter((provider) => provider.type === "password")
        .map((provider) => (
          <section key={provider.id} aria-label={provider.label}>
            <LoginForm onLogin={onLogin} />
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
        pending={redirect.isPending}
        onSelect={(id) => void start(id)}
      />
      {redirect.isError && (
        <p role="alert">Cannot start provider sign-in. Try again later.</p>
      )}
      {methods.length === 0 && (
        <p>No sign-in methods are configured. Contact your administrator.</p>
      )}
    </>
  );
}
