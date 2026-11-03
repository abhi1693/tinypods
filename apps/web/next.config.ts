import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: fileURLToPath(new URL("../../", import.meta.url)),
  transpilePackages: ["@tinypods/ui", "@tinypods/http"],
  poweredByHeader: false,
  logging: { incomingRequests: { ignore: [/^\/setup(?:[/?]|$)/] } },
  async headers() {
    return [
      {
        source: "/setup",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "Cache-Control", value: "no-store" },
        ],
      },
    ];
  },
  allowedDevOrigins: process.env.TINYPODS_WEB_DEV_HOST
    ? [process.env.TINYPODS_WEB_DEV_HOST]
    : [],
};

export default nextConfig;
