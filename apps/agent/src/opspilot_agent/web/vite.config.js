import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backend = env.AGENT_API_ORIGIN || "http://127.0.0.1:8000";
  const knowledge = env.KNOWLEDGE_API_ORIGIN || "http://127.0.0.1:8200";
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
    },
    server: {
      proxy: {
        ...Object.fromEntries(
          ["/session", "/agent/runs", "/approvals", "/audit"].map((path) => [
            path,
            backend,
          ]),
        ),
        "/knowledge/ask": knowledge,
      },
    },
  };
});
