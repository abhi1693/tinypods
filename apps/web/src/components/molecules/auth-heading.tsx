import { BrandMark } from "../atoms/brand-mark";

export function AuthHeading({
  mode,
}: {
  mode: "login" | "register" | "setup";
}) {
  return (
    <h1 className="flex items-center justify-center gap-2 text-lg font-semibold tracking-tight">
      <span>
        {
          {
            setup: "Set up ",
            register: "Register with ",
            login: "Sign in to ",
          }[mode]
        }
      </span>
      <span className="inline-flex items-center gap-1.5">
        <BrandMark className="h-5 w-5 shrink-0" />
        <span>TinyPods</span>
      </span>
    </h1>
  );
}
