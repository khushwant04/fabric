import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingExcludes: { "/*": ["./.env", "./.env.*"] },
  experimental: {
    // Reuse visited pages briefly; explicit prefetch uses Next's minimum static
    // window. LiveRefresh independently checks current account state every 8s.
    staleTimes: { dynamic: 10, static: 30 },
  },
};

export default nextConfig;
