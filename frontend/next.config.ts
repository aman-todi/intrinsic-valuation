import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required by infra/docker/Dockerfile.frontend (§11.1).
  output: "standalone",
  reactStrictMode: true,
  // Don't let `next dev` write AGENTS.md/CLAUDE.md into the package; repo-level CLAUDE.md covers it.
  agentRules: false,
};

export default nextConfig;
