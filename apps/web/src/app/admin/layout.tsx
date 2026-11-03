import type { Metadata } from "next";
import type { ReactNode } from "react";
import { OperatorShell } from "@/components/templates/operator-shell";

export const metadata: Metadata = { title: "TinyPods Admin" };

export default function AdminLayout({ children }: { children: ReactNode }) {
  return <OperatorShell>{children}</OperatorShell>;
}
