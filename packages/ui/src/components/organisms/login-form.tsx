"use client";

import { useId, useRef, type FormEvent } from "react";
import { Button } from "../atoms/button";
import { FormField } from "../molecules/form-field";

export interface LoginFormProps {
  pending: boolean;
  mode?: "login" | "register" | "setup";
  disabled?: boolean;
  error?: string;
  fieldErrors?: Partial<
    Record<"username" | "email" | "password" | "password_confirmation", string>
  >;
  onSubmit: (credentials: {
    username: string;
    email?: string;
    password: string;
    password_confirmation?: string;
  }) => Promise<void>;
}

/** Shared credential UI; the caller owns authentication, errors and session state. */
export function LoginForm({
  pending,
  mode = "login",
  disabled = false,
  error,
  fieldErrors,
  onSubmit,
}: LoginFormProps) {
  const id = useId();
  const submitting = useRef(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || disabled || submitting.current) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    submitting.current = true;
    try {
      await onSubmit({
        username: String(fields.get("username") ?? ""),
        password: String(fields.get("password") ?? ""),
        ...(mode === "setup"
          ? { email: String(fields.get("email") ?? "") }
          : {}),
        ...(mode !== "login"
          ? {
              password_confirmation: String(
                fields.get("password_confirmation") ?? "",
              ),
            }
          : {}),
      });
      form.reset();
    } catch {
      // The caller supplies the error; clear sensitive input before a retry.
      const password = form.elements.namedItem("password");
      for (const name of ["password", "password_confirmation"]) {
        const field = form.elements.namedItem(name);
        if (field instanceof HTMLInputElement) field.value = "";
      }
      requestAnimationFrame(() => {
        const field =
          form.querySelector<HTMLElement>('[aria-invalid="true"]') ?? password;
        if (field instanceof HTMLElement) field.focus();
      });
    } finally {
      submitting.current = false;
    }
  }
  return (
    <form
      onSubmit={submit}
      noValidate
      aria-busy={pending}
      className="mt-6 space-y-4"
    >
      <FormField
        label={mode === "setup" ? "Email" : "Email or username"}
        id={id + "-username"}
        name={mode === "setup" ? "email" : "username"}
        autoComplete={mode === "setup" ? "email" : "username"}
        type={mode === "setup" ? "email" : "text"}
        aria-required="true"
        error={mode === "setup" ? fieldErrors?.email : fieldErrors?.username}
        disabled={pending || disabled}
      />
      <FormField
        label="Password"
        id={id + "-password"}
        name="password"
        type="password"
        autoComplete={mode !== "login" ? "new-password" : "current-password"}
        aria-required="true"
        error={fieldErrors?.password}
        disabled={pending || disabled}
      />
      {mode !== "login" && (
        <FormField
          label="Confirm password"
          id={id + "-confirmation"}
          name="password_confirmation"
          type="password"
          autoComplete="new-password"
          aria-required="true"
          error={fieldErrors?.password_confirmation}
          disabled={pending || disabled}
        />
      )}
      {error && (
        <p className="text-sm text-red-700" role="alert">
          {error}
        </p>
      )}
      <Button
        type="submit"
        loading={pending}
        disabled={disabled}
        className="bg-foreground text-background hover:bg-foreground/90"
      >
        {
          {
            login: "Sign in",
            register: "Create account",
            setup: "Finish setup",
          }[mode]
        }
      </Button>
    </form>
  );
}
