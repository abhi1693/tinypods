import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  transpilePackages: ["@tinypods/ui", "@tinypods/http"],
  poweredByHeader: false,
};

export default nextConfig;
