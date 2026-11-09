import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// In dev the UI runs on :5173 and proxies /api to the Python API (uvicorn newspaper_pipeline.api.server:app --port 8000).
const API = process.env.VITE_API_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': { target: API, changeOrigin: true } } },
  test: { environment: 'jsdom', globals: true, setupFiles: ['./src/test/setup.ts'], css: false },
});
