"use client";

import { useId, useRef, type FormEvent } from "react";
import { Button } from "../atoms/button";
import { FormField } from "../molecules/form-field";

export interface LoginFormProps {
  pending: boolean;
  error?: string;
  fieldErrors?: Partial<Record<"username" | "password", string>>;
  onSubmit: (credentials: { username: string; password: string }) => Promise<void | boolean>;
}

/** Shared credential UI; the caller owns authentication, errors and session state. */
export function LoginForm({ pending, error, fieldErrors, onSubmit }: LoginFormProps) {
  const id = useId();
  const submitting = useRef(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || submitting.current) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    submitting.current = true;
    try {
      const accepted = await onSubmit({
        username: String(fields.get("username") ?? ""),
        password: String(fields.get("password") ?? ""),
      });
      if (accepted !== false) form.reset();
    } catch {
      // The caller supplies the error; clear sensitive input before a retry.
      const password = form.elements.namedItem("password");
      if (password instanceof HTMLInputElement) password.value = "";
    } finally {
      submitting.current = false;
    }
  }
  return <form onSubmit={submit} noValidate aria-busy={pending} className="mt-6 space-y-4">
    <FormField label="Username" id={id + "-username"} name="username" autoComplete="username"
      required error={fieldErrors?.username} disabled={pending} />
    <FormField label="Password" id={id + "-password"} name="password" type="password"
      autoComplete="current-password" required error={fieldErrors?.password} disabled={pending} />
    {error && <p className="text-sm text-red-700" role="alert">{error}</p>}
    <Button type="submit" disabled={pending} className="bg-foreground text-background hover:bg-foreground/90">{pending ? "Signing in…" : "Sign in"}</Button>
  </form>;
}
