import { AuthShell } from "@tinypods/ui/components/templates/auth-shell";
import { Brand } from "@/components/molecules/brand";
import { LoginPanel } from "@/components/organisms/login-panel";

export default function Home() {
  return (
    <AuthShell brand={<Brand />}>
      <LoginPanel />
    </AuthShell>
  );
}
