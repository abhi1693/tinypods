import { AuthShell } from "@tinypods/ui/components/templates/auth-shell";
import { LoginPanel } from "@/components/organisms/login-panel";

export default function RegisterPage() {
  return (
    <AuthShell>
      <LoginPanel mode="register" />
    </AuthShell>
  );
}
