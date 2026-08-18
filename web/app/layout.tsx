import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Sans_Condensed } from "next/font/google";
import "./globals.css";

// next/font downloads these at build time and serves them from our own origin.
// That matters more than usual here: the console is served by a Databricks App
// behind an OAuth proxy, and a runtime request to a font CDN is one more thing
// that can be blocked between the browser and a page that must render.
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
  display: "swap",
});

const cond = IBM_Plex_Sans_Condensed({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-cond",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
  display: "swap",
});

// public/ files keep their own paths, and Flask mounts the export under /static,
// so the icon has to be declared with the prefix rather than left to Next's
// app/icon convention - that emits a root-relative /icon.svg which 404s here.
const ASSET_PREFIX = process.env.NEXT_PUBLIC_ASSET_PREFIX ?? "";

export const metadata: Metadata = {
  icons: { icon: `${ASSET_PREFIX}/icon.svg` },
  title: "Weather Intelligence — semantic search over NWS",
  description:
    "Vector search over National Weather Service alerts and forecasts, stored in "
    + "Databricks Lakebase with pgvector and plotted on a globe.",
};

export const viewport: Viewport = {
  themeColor: "#060b12",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${cond.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
