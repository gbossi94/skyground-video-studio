import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The app is served by the Python application at /app, so the build lands in
// web/app and every asset path is relative to that prefix. In development the
// dev server proxies the API instead, which keeps the session cookie working.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "../web/app", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:4173",
      "/media": "http://127.0.0.1:4173",
    },
  },
});
