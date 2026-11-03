import { Card } from "@tinypods/ui/components/card";
import { Brand } from "@/components/molecules/brand";

export default function Home() {
  return (
    <div className="mx-auto flex min-h-svh max-w-6xl flex-col px-6 sm:px-10">
      <header className="py-7"><Brand /></header>
      <main id="main" className="flex flex-1 items-center justify-center pb-16">
        <Card className="w-full max-w-xl overflow-hidden">
          <div className="h-1.5 bg-accent" />
          <div className="px-8 py-12 sm:px-12">
            <p className="mb-4 text-xs font-semibold uppercase tracking-[0.18em] text-muted-foreground">TinyPods console</p>
            <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">A place for your applications.</h1>
            <p className="mt-4 text-base leading-7 text-muted-foreground">Keep things simple.</p>
          </div>
        </Card>
      </main>
      <footer className="pb-6 text-xs text-muted-foreground">TinyPods</footer>
    </div>
  );
}
