import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The app is served by the Python application at /app, so the build lands in
// web/app and every asset path is relative to that prefix. In development the
// dev server proxies the API instead, which keeps the session cookie working.
// `SKYGROUND_API` points the dev server at another studio — the production
// one, say — to work on the editor against real footage without a local
// worker. The session cookie is set by that studio and, on localhost, Chrome
// sends it back even when it is marked Secure.
const studio = process.env.SKYGROUND_API ?? "http://127.0.0.1:4173";
const proxy = { target: studio, changeOrigin: true, secure: true, cookieDomainRewrite: "" };

export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "../web/app", emptyOutDir: true },
  server: {
    port: 5173,
    // The panel at the root too, so signing in against that studio works
    // from here: its form sets the cookie the editor then sends along.
    proxy: { "/api": proxy, "/media": proxy, "^/(?!app(/|$)|@|src/|node_modules/)": proxy },
  },
});
