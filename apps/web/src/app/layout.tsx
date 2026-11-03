import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "@tinypods/web-auth/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "TinyPods",
  description: "A simple place for your applications.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
