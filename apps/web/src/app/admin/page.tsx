"use client";

import { useAuthProvidersList } from "@tinypods/http/client";
import { Card } from "@tinypods/ui/components/card";
import { RequestFeedback } from "@tinypods/ui/components/molecules/request-feedback";

export default function ConfigurationPage() {
  const providers = useAuthProvidersList({
    query: { retry: false, gcTime: 0 },
    fetch: { cache: "no-store" },
  });

  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight">Configuration</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Review how people can access this instance.
      </p>
      <Card className="mt-6 overflow-hidden border-border">
        <section aria-labelledby="access-heading">
          <div className="border-b border-border px-5 py-4">
            <h2 id="access-heading" className="font-semibold">
              Account access
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              These settings are managed in the TinyPods deployment environment.
            </p>
          </div>
          <div className="px-5 py-4">
            {providers.isPending && (
              <RequestFeedback pending message="Loading account access…" />
            )}
            {providers.error && (
              <RequestFeedback
                message="Could not load account access."
                pending={providers.isFetching}
                onRetry={() => {
                  void providers.refetch();
                }}
              />
            )}
            {providers.data && !providers.error && (
              <dl className="divide-y divide-border text-sm">
                <div className="grid gap-1 pb-4 sm:grid-cols-2">
                  <dt className="font-medium">Public registration</dt>
                  <dd>
                    {providers.data.data.registration_allowed
                      ? "Open"
                      : "Closed"}
                  </dd>
                </div>
                <div className="grid gap-1 pt-4 sm:grid-cols-2">
                  <dt className="font-medium">Sign-in methods</dt>
                  <dd>
                    {providers.data.data.data.length ? (
                      <ul className="space-y-1">
                        {providers.data.data.data.map((provider) => (
                          <li key={provider.id}>{provider.label}</li>
                        ))}
                      </ul>
                    ) : (
                      "None configured"
                    )}
                  </dd>
                </div>
              </dl>
            )}
          </div>
        </section>
      </Card>
    </>
  );
}
