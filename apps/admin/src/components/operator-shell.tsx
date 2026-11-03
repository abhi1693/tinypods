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
          Sign in to TinyPods Admin
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
          This account does not have access to TinyPods Admin.
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
    <div className="min-h-dvh md:grid md:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="border-b border-border bg-card px-5 py-4 md:border-r md:border-b-0 md:py-6">
        <div className="flex items-center gap-2.5 font-semibold">
          <BrandMark className="size-7" />
          <span>TinyPods Admin</span>
        </div>
        <nav aria-label="Operator navigation" className="mt-5 md:mt-8">
          <Link
            href="/"
            aria-current="page"
            className="block rounded-lg bg-accent/30 px-3 py-2 text-sm font-semibold"
          >
            Configuration
          </Link>
        </nav>
      </aside>
      <div className="min-w-0">
        <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border bg-card px-5 py-4 text-sm md:px-8">
          {webOrigin && (
            <Link href={webOrigin} className="underline underline-offset-4">
              Back to TinyPods
            </Link>
          )}
          <p className="min-w-0 break-words text-muted-foreground">
            <span className="font-semibold text-foreground">
              {session.data.data.user.username}
            </span>
            <span className="ml-2">Instance owner</span>
          </p>
        </header>
        <main className="mx-auto max-w-5xl px-5 py-8 md:px-8 md:py-10">
          {children}
        </main>
      </div>
    </div>
  );
}
