import type { ErrorResponse } from "./generated/client/schemas/models";

/** Map backend-owned field feedback; no form rules or validation messages live here. */
export function getValidationFeedback(
  error: { info?: ErrorResponse | void } | null | undefined,
) {
  const info = error?.info;
  if (!info || info.code !== "validation_error") return undefined;
  const fields: Record<string, string> = {};
  for (const issue of info.issues ?? []) {
    if (issue.location[0] !== "body" || !issue.message) continue;
    const field = issue.location.slice(1).join(".");
    if (field && !fields[field]) fields[field] = issue.message;
  }
  return { fields, message: info.message };
}

/** Preserve deliberate API messages; translate only generic HTTP/network failures. */
export function getRequestErrorMessage(
  error: unknown,
  fallback: string,
  statusMessages: Partial<Record<number, string>> = {},
): string {
  if (!error || typeof error !== "object") return fallback;
  if ("info" in error && error.info && typeof error.info === "object") {
    const info = error.info;
    if (
      "code" in info &&
      typeof info.code === "string" &&
      "message" in info &&
      typeof info.message === "string" &&
      !info.code.startsWith("http_")
    )
      return info.message;
  }
  if ("status" in error && typeof error.status === "number")
    return statusMessages[error.status] ?? fallback;
  return fallback;
}
