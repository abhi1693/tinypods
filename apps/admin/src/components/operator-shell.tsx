"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useAuthOwnerSessionGet } from "@tinypods/http/client";
import { RequestFeedback } from "@tinypods/ui/components/molecules/request-feedback";
import { AuthShell } from "@tinypods/ui/components/templates/auth-shell";
import { BrandMark } from "@tinypods/ui/components/atoms/brand-mark";
import { LoginMethods } from "@tinypods/web-auth/login-methods";

const webOrigin = process.env.NEXT_PUBLIC_TINYPODS_WEB_ORIGIN;

export function OperatorShell({ children }: { children: ReactNode }) {
  const session = useAuthOwnerSessionGet({
    query: { retry: false, gcTime: 0, refetchOnMount: "always" },
    fetch: { cache: "no-store" },
  });

  if (session.isFetching) {
    return (
      <AuthShell>
        <RequestFeedback pending message="Checking operator access…" />
      </AuthShell>
    );
  }
  if (session.error?.status === 401) {
    return (
      <AuthShell>
        <h1 className="text-center text-lg font-semibold">
          Sign in to TinyPods Admin Portal
        </h1>
        <p className="mt-2 text-center text-sm text-muted-foreground">
          Use an instance owner account.
        </p>
        <LoginMethods
          operator
          onLogin={() => {
            void session.refetch();
          }}
        />
      </AuthShell>
    );
  }
  if (session.error?.status === 403) {
    return (
      <AuthShell>
        <h1 className="text-lg font-semibold">Owner access required</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          This account does not have access to TinyPods Admin Portal.
        </p>
        {webOrigin && (
          <Link
            className="mt-5 inline-block text-sm underline underline-offset-4"
            href={webOrigin}
          >
            Back to TinyPods
          </Link>
        )}
      </AuthShell>
    );
  }
  if (!session.data || session.error) {
    return (
      <AuthShell>
        <RequestFeedback
          message="Could not verify operator access."
          onRetry={() => {
            void session.refetch();
          }}
        />
      </AuthShell>
    );
  }

  return (
    <div className="min-h-dvh">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-4 md:px-8">
          <div className="flex items-center gap-2.5 font-semibold">
            <BrandMark className="size-7" />
            <span>TinyPods Admin Portal</span>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 text-sm">
            <p className="min-w-0 break-words text-muted-foreground">
              <span className="font-semibold text-foreground">
                {session.data.data.user.username}
              </span>
              <span className="ml-2">Instance owner</span>
            </p>
            {webOrigin && (
              <Link href={webOrigin} className="underline underline-offset-4">
                Back to TinyPods
              </Link>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 md:px-8">{children}</main>
    </div>
  );
}
