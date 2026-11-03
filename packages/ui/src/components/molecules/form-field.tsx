import type { ComponentProps } from "react";
import { Input } from "../atoms/input";

export function FormField({
  label,
  id,
  error,
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
        id={id}
        aria-invalid={!!error}
        aria-describedby={error ? id + "-error" : undefined}
        {...props}
      />
      {error && (
        <p className="mt-1.5 text-sm text-red-700" id={id + "-error"}>
          {error}
        </p>
      )}
    </div>
  );
}
