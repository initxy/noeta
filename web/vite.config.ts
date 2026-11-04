/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

// `npm run dev` proxies the API to a running `noeta serve` (NOETA_SERVER,
// default http://127.0.0.1:8642). The Origin header is dropped so the server
// takes the proxied requests as its own page's.
const server = process.env.NOETA_SERVER ?? "http://127.0.0.1:8642";
const api = ["/tasks", "/summaries", "/files", "/commands", "/agents", "/models"];

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { chunkSizeWarningLimit: 1500 },
  server: {
    proxy: Object.fromEntries(
      api.map((path): [string, ProxyOptions] => [
        path,
        {
          target: server,
          changeOrigin: true,
          configure: (proxy) => proxy.on("proxyReq", (req) => req.removeHeader("origin")),
        },
      ]),
    ),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
