import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ortomosaico",
  description: "Processamento de imagens aéreas: um voo, um dataset, um ortomosaico",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR">
      <body>
        <div className="shell">
          <aside className="sidebar">
            <div className="brand">
              <div className="brand-mark" />
              <div>
                Ortomosaico
                <small>voo completo, um mosaico</small>
              </div>
            </div>
            <nav className="nav">
              <Link href="/">Dashboard</Link>
              <Link href="/projects">Projetos</Link>
              <Link href="/projects/new">Novo processamento</Link>
            </nav>
            <div className="spacer" />
            <p className="muted" style={{ fontSize: 11, lineHeight: 1.5 }}>
              A pasta raiz selecionada é o voo. Todas as subpastas entram no mesmo
              dataset e geram um único ortomosaico georreferenciado.
            </p>
          </aside>
          <main className="main">{children}</main>
        </div>
      </body>
    </html>
  );
}
