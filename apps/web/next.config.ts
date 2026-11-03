import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: fileURLToPath(new URL("../../", import.meta.url)),
  transpilePackages: ["@tinypods/ui", "@tinypods/http"],
  poweredByHeader: false,
};

export default nextConfig;
