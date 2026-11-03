import type { ComponentProps } from "react";
import { Input } from "../atoms/input";

export function FormField({
  label,
  id,
  error,
  "aria-describedby": describedBy,
  ...props
}: ComponentProps<"input"> & {
  label: string;
  id: string;
  error?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block text-sm font-medium">
        {label}
      </label>
      <Input
        {...props}
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={
          [describedBy, error && id + "-error"].filter(Boolean).join(" ") ||
          undefined
        }
      />
      <div className="min-h-5 pt-1">
        {error && (
          <p
            className="flex items-start gap-1.5 text-xs leading-4 text-red-800"
            id={id + "-error"}
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              className="mt-0.5 size-3 shrink-0"
            >
              <circle cx="8" cy="8" r="6" />
              <path d="M8 5v3.5m0 2v.5" strokeLinecap="round" />
            </svg>
            <span className="min-w-0 break-words">{error}</span>
          </p>
        )}
      </div>
    </div>
  );
}
