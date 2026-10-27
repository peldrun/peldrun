// frontend/next.config.mjs

/**
 * Backend origin used by Next.js rewrites.
 *
 * All frontend API calls are written as relative URLs ("/api/...") and
 * proxied here. Keeping this configuration in one place means operators
 * can point the frontend at any backend (localhost, LAN, container,
 * remote) without touching component code.
 *
 * Override precedence (highest first):
 *   1. PELDRUN_BACKEND_URL      - preferred for CI/CD and Docker builds
 *   2. NEXT_PUBLIC_BACKEND_URL  - kept for parity with public env vars
 *   3. DEFAULT_BACKEND_URL      - documented local-development fallback
 *
 * Example:
 *   PELDRUN_BACKEND_URL=http://peldrun-backend:8088 npm run build
 */
const DEFAULT_BACKEND_URL = "http://127.0.0.1:8088";

const rawBackendUrl =
  process.env.PELDRUN_BACKEND_URL ||
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  DEFAULT_BACKEND_URL;

// Normalize: strip any trailing slashes so the rewrite destination
// never produces "//api/..." paths.
const BACKEND_URL = rawBackendUrl.replace(/\/+$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: false,
  experimental: {
    workerThreads: false,
    cpus: 1,
    agentUpgrade: "latest",
  },
  // Expose the resolved backend URL to the browser bundle so the
  // diagnostic card can display what the rewrites are actually
  // pointing at. This is a display-only convenience: the rewrites
  // themselves read from the same BACKEND_URL constant above, so the
  // value shown to the user is guaranteed to match the routing.
  env: {
    NEXT_PUBLIC_PELDRUN_BACKEND_URL: BACKEND_URL,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
