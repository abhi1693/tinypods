import Link from "next/link";
import { BrandMark } from "@tinypods/ui/components/atoms/brand-mark";

export function Brand() {
  return (
    <Link
      href="/"
      className="inline-flex items-center gap-2.5 rounded-md font-semibold tracking-tight focus-visible:outline-2 focus-visible:outline-offset-4"
    >
      <BrandMark />
      TinyPods
    </Link>
  );
}
