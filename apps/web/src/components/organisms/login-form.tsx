"use client";

import { useState } from "react";
import { LoginRequest as loginRequestSchema } from "@tinypods/http/validation";
import { useAuthLocalLogin } from "@tinypods/http/client";
import type { LoginResponse } from "@tinypods/http/models";
import { LoginForm as SharedLoginForm } from "@tinypods/ui/components/organisms/login-form";

export function LoginForm({ onLogin }: { onLogin: (identity: LoginResponse) => void }) {
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<"username" | "password", string>>>({});
  const login = useAuthLocalLogin({
    mutation: { retry: false, gcTime: 0 },
  });
  const status = login.error?.status;
  const error = status === 401 ? "The username or password was not accepted."
    : status === 429 ? "Too many sign-in attempts. Wait a minute before trying again."
    : status === 403 ? "This sign-in request could not be verified. Reload and try again."
    : status === 422 ? "Check your username and password format."
    : status === 404 ? "Local sign-in is not available on this instance."
    : "Cannot reach the sign-in service. Check your connection and try again.";
  return <SharedLoginForm pending={login.isPending} fieldErrors={fieldErrors} error={login.isError ? error : undefined}
    onSubmit={async (credentials) => {
      login.reset();
      const parsed = loginRequestSchema.safeParse(credentials);
      if (!parsed.success) {
        const errors: Partial<Record<"username" | "password", string>> = {};
        for (const issue of parsed.error.issues) {
          const field = issue.path[0];
          if ((field === "username" || field === "password") && !errors[field]) errors[field] = issue.message;
        }
        setFieldErrors(errors);
        return false;
      }
      setFieldErrors({});
      const response = await login.mutateAsync({ data: parsed.data });
      onLogin(response.data);
    }} />;
}
