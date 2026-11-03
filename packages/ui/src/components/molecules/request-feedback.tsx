import { Button } from "../atoms/button";

/** Accessible request state and a shared, optional retry control. */
export function RequestFeedback({
  message,
  pending = false,
  onRetry,
}: {
  message: string;
  pending?: boolean;
  onRetry?: () => void;
}) {
  return (
    <div className="text-sm">
      <p role={pending ? "status" : "alert"}>{message}</p>
      {onRetry && (
        <Button className="mt-4" loading={pending} onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
