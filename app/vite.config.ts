import { defineConfig } from "vite";

// The build is served by the Python engine (mnemoloci/web), same origin as the API.
export default defineConfig({
  base: "./",
  build: { outDir: "../mnemoloci/web", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
});
