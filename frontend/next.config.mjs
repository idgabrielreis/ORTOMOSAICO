/** @type {import('next').NextConfig} */
const apiTarget = process.env.API_URL ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  // O progresso do processamento chega por Server-Sent Events. Com a compressão
  // do Next ligada, os eventos ficam presos no buffer do gzip e a tela só
  // atualiza no fim: a barra congela e tempo, CPU e RAM aparecem vazios.
  compress: false,
  // O frontend fala com a API pelo mesmo origin: evita CORS e deixa o deploy
  // atrás de um único proxy reverso.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiTarget}/api/:path*` }];
  },
};

export default nextConfig;
