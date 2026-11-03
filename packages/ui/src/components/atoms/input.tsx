import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      {...props}
      className={cn(
        "h-10 w-full rounded-lg border border-border bg-card px-3 text-sm outline-none transition-shadow focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50",
        className,
      )}
    />
  );
}
