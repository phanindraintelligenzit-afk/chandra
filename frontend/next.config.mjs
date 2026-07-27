/** @type {import('next').NextConfig} */
const repositoryName = process.env.GITHUB_REPOSITORY?.split("/")[1];
const pagesPath = process.env.GITHUB_ACTIONS && repositoryName ? `/${repositoryName}` : "";
const isCiExport = process.env.GITHUB_ACTIONS === "true";
const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:6001";
const internalApiUrl = process.env.INTERNAL_API_URL ?? "http://127.0.0.1:6001";

const nextConfig = {
  ...(isCiExport ? { output: "export" } : {}),
  trailingSlash: true,
  skipTrailingSlashRedirect: true,
  basePath: pagesPath || undefined,
  assetPrefix: pagesPath || undefined,
  env: {
    NEXT_PUBLIC_BASE_PATH: pagesPath,
    NEXT_PUBLIC_API_URL: apiUrl,
    NEXT_PUBLIC_AWS_REGION: process.env.AWS_DEFAULT_REGION || process.env.AWS_REGION || "us-east-1"
  },
  images: {
    unoptimized: true
  },
  experimental: {
    proxyTimeout: 600_000
  },
  ...(isCiExport
    ? {}
    : {
        async rewrites() {
          return {
            beforeFiles: [
              { source: "/api/backend/:path*/", destination: `${internalApiUrl}/:path*` },
              { source: "/api/backend/:path*", destination: `${internalApiUrl}/:path*` }
            ],
            afterFiles: [],
            fallback: []
          };
        }
      })
};

export default nextConfig;
