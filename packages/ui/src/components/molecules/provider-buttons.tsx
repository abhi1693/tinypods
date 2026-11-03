"use client";
import { Button } from "../atoms/button";

export interface ProviderOption { id: string; label: string }
export function ProviderButtons({ options, pending, onSelect }: {
  options: ProviderOption[];
  pending: boolean;
  onSelect: (id: string) => void;
}) {
  return <div className="mt-6 flex flex-col gap-3">
    {options.map(option => <Button key={option.id} type="button" disabled={pending}
      onClick={() => onSelect(option.id)}>Continue with {option.label}</Button>)}
    {pending && <p role="status">Opening sign-in provider…</p>}
  </div>;
}
