"use client";

import { useAuthLocalLogin } from "@tinypods/http/client";
import {
  getRequestErrorMessage,
  getValidationFeedback,
} from "@tinypods/http/errors";
import type { LoginResponse } from "@tinypods/http/models";
import { LoginForm as SharedLoginForm } from "@tinypods/ui/components/organisms/login-form";

const loginStatusMessages = {
  401: "The username or password was not accepted.",
  429: "Too many sign-in attempts. Wait a minute before trying again.",
  403: "This sign-in request could not be verified. Reload and try again.",
  404: "Local sign-in is not available on this instance.",
};

export function LoginForm({
  onLogin,
  disabled = false,
  onPendingChange,
}: {
  onLogin: (identity: LoginResponse) => void;
  disabled?: boolean;
  onPendingChange?: (pending: boolean) => void;
}) {
  const login = useAuthLocalLogin({ mutation: { retry: false, gcTime: 0 } });
  const validation = getValidationFeedback(login.error);
  const error = getRequestErrorMessage(
    login.error,
    "Cannot reach the sign-in service. Check your connection and try again.",
    loginStatusMessages,
  );
  return (
    <SharedLoginForm
      pending={login.isPending}
      disabled={disabled}
      fieldErrors={validation?.fields}
      error={login.isError ? error : undefined}
      onSubmit={async (credentials) => {
        login.reset();
        onPendingChange?.(true);
        try {
          const response = await login.mutateAsync({ data: credentials });
          onLogin(response.data);
        } finally {
          onPendingChange?.(false);
        }
      }}
    />
  );
}
