import type { ReactNode } from "react";

/** A compact, centered authentication surface reusable by each portal. */
export function AuthShell({ brand, children }: { brand: ReactNode; children: ReactNode }) {
  return <main className="flex min-h-svh items-center justify-center px-6 py-12">
    <div className="w-full max-w-xs">
      <div className="mb-7 flex justify-center">{brand}</div>
      {children}
    </div>
  </main>;
}
