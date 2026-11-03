import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "@tinypods/web-auth/providers";
import { OperatorShell } from "@/components/operator-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "TinyPods Admin Portal",
  description: "Administration for TinyPods.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">
        <Providers>
          <OperatorShell>{children}</OperatorShell>
        </Providers>
      </body>
    </html>
  );
}
