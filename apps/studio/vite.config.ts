import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    fs: {
      deny: [".env", "**/.env*", "**/.git/**", "**/.vks/**", "*.{crt,pem,key}"],
    },
    proxy: {
      "/api": {
        target:
          loadEnv(mode, ".", "VKS_").VKS_DEV_API_TARGET ||
          "http://127.0.0.1:8014",
        changeOrigin: false,
      },
    },
  },
}));
