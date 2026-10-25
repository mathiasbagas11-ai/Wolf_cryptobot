import "./globals.css";
import Link from "next/link";

export const metadata = { title: "Wolf Dashboard", description: "Paper ledger of the Wolf crypto bot" };
export const viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }) {
  return (
    <html lang="id">
      <body>
        <header className="top">
          <span className="brand">🐺 Wolf</span>
          <nav>
            <Link href="/">Overview</Link>
            <Link href="/signals">Sinyal aktif</Link>
          </nav>
        </header>
        <main>{children}</main>
        <footer>Paper ledger — bot ini tidak pernah menaruh order. Bukan saran investasi.</footer>
      </body>
    </html>
  );
}
