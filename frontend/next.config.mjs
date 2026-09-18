/** @type {import('next').NextConfig} */
// 後端預設 8014;BACKEND_PORT 環境變數可覆寫。
const BACKEND_PORT = process.env.BACKEND_PORT || '8014'
const nextConfig = {
  reactStrictMode: false,
  async rewrites() {
    return [
      {
        source: '/api/backend/:path*',
        destination: `http://localhost:${BACKEND_PORT}/:path*`,
      },
    ]
  },
}

export default nextConfig
