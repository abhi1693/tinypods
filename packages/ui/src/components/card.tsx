import type { ComponentProps } from "react";
import { cn } from "../lib/utils";

/** Shared native card primitive using the shadcn className/ref convention. */
export function Card({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      className={cn(
        "rounded-2xl border bg-card text-card-foreground shadow-sm",
        className,
      )}
      {...props}
    />
  );
}
