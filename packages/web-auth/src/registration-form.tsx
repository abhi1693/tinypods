"use client";

import { useAuthLocalRegister } from "@tinypods/http/client";
import {
  getRequestErrorMessage,
  getValidationFeedback,
} from "@tinypods/http/errors";
import { LoginForm as SharedLoginForm } from "@tinypods/ui/components/organisms/login-form";

export function RegistrationForm({
  onRegistered,
  disabled = false,
  onPendingChange,
}: {
  onRegistered: () => void;
  disabled?: boolean;
  onPendingChange?: (pending: boolean) => void;
}) {
  const registration = useAuthLocalRegister({
    mutation: { retry: false, gcTime: 0 },
  });
  const feedback = getValidationFeedback(registration.error);
  const message = getRequestErrorMessage(
    registration.error,
    "Cannot reach registration. Check your connection and try again.",
    {
      403: "Registration is closed or this request could not be verified. Reload and try again.",
      429: "Too many registration attempts. Wait a minute and try again.",
    },
  );
  return (
    <SharedLoginForm
      mode="register"
      pending={registration.isPending}
      disabled={disabled}
      fieldErrors={feedback?.fields}
      error={registration.isError ? message : undefined}
      onSubmit={async (values) => {
        registration.reset();
        onPendingChange?.(true);
        try {
          await registration.mutateAsync({
            data: {
              username: values.username,
              password: values.password,
              password_confirmation: values.password_confirmation ?? "",
            },
          });
          onRegistered();
        } finally {
          onPendingChange?.(false);
        }
      }}
    />
  );
}
