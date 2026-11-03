"use client";

import { Button } from "../atoms/button";
import { ProviderIcon } from "../atoms/provider-icon";

export interface ProviderOption {
  id: string;
  label: string;
  iconUrl?: string | null;
  iconLabel?: string;
}
export function ProviderButtons({
  options,
  pendingId,
  disabled = false,
  onSelect,
}: {
  options: ProviderOption[];
  pendingId: string | null;
  disabled?: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="mt-6 flex flex-col gap-3">
      {options.map((option) => (
        <Button
          key={option.id}
          type="button"
          loading={pendingId === option.id}
          disabled={disabled}
          icon={
            <ProviderIcon
              key={option.iconUrl ?? option.id}
              src={option.iconUrl}
              label={option.iconLabel ?? option.label}
            />
          }
          onClick={() => onSelect(option.id)}
        >
          {option.label}
        </Button>
      ))}
    </div>
  );
}
