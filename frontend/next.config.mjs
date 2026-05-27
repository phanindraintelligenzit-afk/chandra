/** @type {import('next').NextConfig} */
const repositoryName = process.env.GITHUB_REPOSITORY?.split("/")[1];
const pagesPath = process.env.GITHUB_ACTIONS && repositoryName ? `/${repositoryName}` : "";
const isCiExport = process.env.GITHUB_ACTIONS === "true";
const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://184.72.96.181:6002";

const nextConfig = {
  ...(isCiExport ? { output: "export" } : {}),
  trailingSlash: true,
  skipTrailingSlashRedirect: true,
  basePath: pagesPath,
  assetPrefix: pagesPath,
  env: {
    NEXT_PUBLIC_BASE_PATH: pagesPath,
    NEXT_PUBLIC_API_URL: apiUrl
  },
  images: {
    unoptimized: true
  },
  experimental: {
    proxyTimeout: 180_000
  },
  ...(isCiExport
    ? {}
    : {
        async rewrites() {
          return {
            beforeFiles: [
              { source: "/api/backend/:path*/", destination: `${apiUrl}/:path*` },
              { source: "/api/backend/:path*", destination: `${apiUrl}/:path*` }
            ],
            afterFiles: [],
            fallback: []
          };
        }
      })
};

export default nextConfig;
