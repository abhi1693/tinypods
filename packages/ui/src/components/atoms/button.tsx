import type { ComponentProps } from "react";
import { cn } from "../../lib/utils";

export function Button({ className, ...props }: ComponentProps<"button">) {
  return <button {...props} className={cn("h-10 w-full rounded-lg border border-border bg-card px-4 text-sm font-medium transition-colors hover:bg-background focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-foreground disabled:cursor-not-allowed disabled:opacity-50", className)} />;
}
