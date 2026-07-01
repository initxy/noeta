import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  appType: "mpa",
  build: {
    emptyOutDir: true,
    outDir: "dist",
    rollupOptions: {
      input: {
        chat: resolve(__dirname, "chat.html"),
        trace: resolve(__dirname, "trace.html"),
      },
    },
  },
  server: {
    // Dev (`npm run dev`): forward the new thin-backend protocol to the Python
    // server on :8765. Production serves this SPA same-origin from that backend
    // (noeta.agent.backend.static_assets), so these paths only matter for dev.
    proxy: {
      "/capabilities": "http://127.0.0.1:8765",
      "/tasks": "http://127.0.0.1:8765",
      "/stream": "http://127.0.0.1:8765",
      "/content": "http://127.0.0.1:8765",
      "/files": "http://127.0.0.1:8765",
      "/file": "http://127.0.0.1:8765",
      "/mcp": "http://127.0.0.1:8765",
      "/preview": "http://127.0.0.1:8765",
      "/health": "http://127.0.0.1:8765",
    },
  },
});
