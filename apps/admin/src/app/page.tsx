import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Dashboard | TinyPods Admin Portal",
};

export default function DashboardPage() {
  return (
    <h1 className="py-8 text-2xl font-semibold tracking-tight">Dashboard</h1>
  );
}
