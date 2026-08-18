/** @type {import('next').NextConfig} */

// Static export: `next build` emits plain HTML/JS that Flask serves. There is no
// Node process in production, which is why the whole console is a client
// component talking to the Flask API over the same origin.
//
// assetPrefix only applies to the production build. Flask keeps its default
// static mount at /static, so the export's chunks have to be requested from
// there; in dev the Next server serves them from the root as usual.
const isProd = process.env.NODE_ENV === "production";

export default {
  // A stray package-lock.json in the home directory otherwise wins the root
  // inference and Turbopack resolves from there.
  turbopack: { root: import.meta.dirname },
  output: "export",
  assetPrefix: isProd ? "/static" : undefined,
  images: { unoptimized: true },
  // Files in public/ keep their own paths, so the globe fetches geo.json
  // through this rather than through assetPrefix.
  env: { NEXT_PUBLIC_ASSET_PREFIX: isProd ? "/static" : "" },
  reactStrictMode: true,
};
