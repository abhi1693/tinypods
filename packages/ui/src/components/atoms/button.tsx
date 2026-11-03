import type { ComponentProps, ReactNode } from "react";
import { cn } from "../../lib/utils";

export interface ButtonProps extends ComponentProps<"button"> {
  loading?: boolean;
  icon?: ReactNode;
}

/** Balanced icon slots keep the label centered during loading. */
export function Button({
  className,
  children,
  disabled,
  loading = false,
  icon,
  ...props
}: ButtonProps) {
  return (
    <button
      {...props}
      data-slot="button"
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        "inline-flex min-h-10 w-full cursor-pointer items-center justify-center rounded-lg border border-muted-foreground/70 bg-card px-4 py-2 text-sm font-medium transition-colors hover:bg-background focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    >
      <span className="grid grid-cols-[1rem_auto_1rem] items-center gap-2">
        <span
          aria-hidden="true"
          className="flex size-4 items-center justify-center"
        >
          {loading ? (
            <span className="size-4 animate-spin rounded-full border-2 border-current border-r-transparent" />
          ) : (
            icon
          )}
        </span>
        <span>{children}</span>
        <span aria-hidden="true" className="size-4" />
      </span>
    </button>
  );
}
