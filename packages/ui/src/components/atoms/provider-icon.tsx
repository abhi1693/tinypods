"use client";

import { useState } from "react";

/** Remote icons are optional decoration; keep a useful, stable fallback. */
export function ProviderIcon({
  src,
  label,
}: {
  src?: string | null;
  label: string;
}) {
  const [failed, setFailed] = useState(false);
  if (!src?.startsWith("https://") || failed) {
    return (
      <span
        aria-hidden="true"
        data-icon-fallback
        className="text-xs font-semibold"
      >
        {label.slice(0, 1).toUpperCase()}
      </span>
    );
  }
  return (
    <span className="flex size-4 shrink-0 overflow-hidden" aria-hidden="true">
      <img
        ref={(image) => {
          // An SSR image can fail before React attaches its error handler.
          if (image?.complete && image.naturalWidth === 0) setFailed(true);
        }}
        src={src}
        alt=""
        height={16}
        className="h-4 w-auto max-w-none shrink-0"
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
      />
    </span>
  );
}
