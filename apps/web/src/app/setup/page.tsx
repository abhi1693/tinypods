import { AuthShell } from "@tinypods/ui/components/templates/auth-shell";
import { SetupPanel } from "@/components/organisms/setup-panel";

export default function SetupPage() {
  return (
    <AuthShell>
      <SetupPanel />
    </AuthShell>
  );
}
