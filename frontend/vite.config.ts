import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const PROXY_PATHS = [
  "/api",
  "/auth",
  "/sessions",
  "/swarm/presets",
  "/swarm/runs",
  "/qveris",
  "/settings/llm",
  "/settings/data-sources",
  "/channels",
  "/mandate",
  "/live",
  "/upload",
  "/shadow-reports",
  "/scheduled-runs",
  "/options",
  "/skills",
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_URL || "http://127.0.0.1:8899";
  const apiProxy = { target: apiTarget, changeOrigin: true };
  const apiProxyWithHtmlFallback = {
    ...apiProxy,
    bypass(req: { headers: { accept?: string } }) {
      if (req.headers.accept?.includes("text/html")) {
        return "/index.html";
      }
    },
  };

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(import.meta.dirname, "./src") },
    },
    server: {
      port: 5899,
      // Bind dual-stack. Vite's default host ("localhost") resolves to
      // IPv6 loopback (::1) first on Windows, so http://127.0.0.1:5899
      // refuses connections. "::" accepts both IPv4 and IPv6.
      host: "::",
      proxy: {
        ...Object.fromEntries(PROXY_PATHS.map((p) => [p, apiProxy])),
        // SPA RunDetail page — only the two-segment ``/runs/{id}``
        // form should fall back to ``index.html`` on browser navigation.
        // ``/runs/{id}/code`` and ``/runs/{id}/pine`` are API-only and
        // must keep proxying to the backend even when Accept is text/html.
        "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
        "/runs": apiProxy,
        "/correlation": apiProxyWithHtmlFallback,
        // /options is both the SPA Options Lab route and an API prefix
        // (/options/payoff, /options/chain) — same dual role as /correlation.
        // Overrides the plain PROXY_PATHS entry above.
        "/options": apiProxyWithHtmlFallback,
        // /skills is both the SPA Skills page and the API prefix the page
        // reads. Without the html fallback a browser refresh or a pasted
        // deep link answered with raw JSON instead of the page; without the
        // proxy at all, the page's own fetch got index.html back and threw
        // "Unexpected token '<'". The split is on Accept: navigations send
        // text/html, `request()` sends none.
        "/skills": apiProxyWithHtmlFallback,
        "^/alpha(?:/|$)": apiProxy,
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: (id: string) => {
            if (/node_modules\/(react|react-dom|react-router)\//.test(id)) return "vendor-react";
            if (/node_modules\/echarts\//.test(id)) return "vendor-charts";
            return undefined;
          },
        },
      },
    },
  };
});
