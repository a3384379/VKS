import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { posix } from "node:path";
export default defineConfig(({ mode }) => {
  const apiTarget =
    loadEnv(mode, ".", "VKS_").VKS_DEV_API_TARGET || "http://127.0.0.1:8000";
  return {
    plugins: [
      react(),
      {
        name: "local-diagram-frame",
        configureServer(server) {
          const frame = readFileSync(
            new URL("./diagram-frame.html", import.meta.url),
            "utf8",
          );
          const publicSources = new Set([
            "/src/diagram-frame.ts",
            "/src/diagram-policy.ts",
            ...["diagram-frame.ts", "diagram-policy.ts"].map(
              (name) =>
                "/@fs" +
                fileURLToPath(
                  new URL(
                    "../../packages/markdown-runtime/src/" + name,
                    import.meta.url,
                  ),
                ),
            ),
          ]);
          server.middlewares.use((request, response, next) => {
            let path: string;
            try {
              path = posix.normalize(
                decodeURIComponent(
                  new URL(request.url || "/", "http://vks.invalid").pathname,
                ),
              );
            } catch {
              next();
              return;
            }
            const cachePath = server.config.cacheDir.replace(/\\/g, "/");
            // The opaque frame may import only these public renderer modules.
            // No CORS permission is added to API/private filesystem responses.
            if (
              publicSources.has(path) ||
              path.startsWith("/node_modules/.vite/") ||
              path.startsWith("/@fs" + cachePath + "/") ||
              path.startsWith("/@fs/" + cachePath + "/")
            )
              response.setHeader("Access-Control-Allow-Origin", "*");
            if (path === "/diagram-frame.html") {
              response.setHeader("Content-Type", "text/html; charset=utf-8");
              response.setHeader("X-Frame-Options", "SAMEORIGIN");
              response.end(frame);
              return;
            }
            next();
          });
        },
      },
    ],
    optimizeDeps: {
      // This dependency is behind a lazy import and must be ready before a
      // reader opens a fenced code block, without a late optimizer reload.
      include: ["lowlight", "mermaid", "@plantuml/core"],
    },
    build: {
      rollupOptions: {
        input: {
          main: fileURLToPath(new URL("./index.html", import.meta.url)),
          diagram: fileURLToPath(
            new URL("./diagram-frame.html", import.meta.url),
          ),
        },
        output: {
          manualChunks: {
            markdown: ["react-markdown", "remark-gfm"],
            prototype: ["htmlparser2"],
          },
        },
      },
    },
    server: {
      fs: {
        deny: [
          ".env",
          ".env.*",
          "**/.env*",
          "*.{crt,pem,key}",
          "**/.git/**",
          "**/.vks/**",
        ],
      },
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: false,
        },
        "/mcp": {
          target: apiTarget,
          changeOrigin: false,
        },
      },
    },
  };
});
