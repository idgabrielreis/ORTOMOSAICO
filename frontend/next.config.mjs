/** @type {import('next').NextConfig} */
const apiTarget = process.env.API_URL ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  // O frontend fala com a API pelo mesmo origin: evita CORS e deixa o deploy
  // atrás de um único proxy reverso.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiTarget}/api/:path*` }];
  },
};

export default nextConfig;
