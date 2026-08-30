import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Allow overriding the backend the dev server proxies /api to — used when
// running the local UI against a remote backend (e.g. a remote VPS via SSH
// tunnel). Default keeps the original local-only behavior.
//   VITE_API_PROXY_TARGET=http://127.0.0.1:18000 yarn dev
const API_PROXY_TARGET =
  process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  // Pin the demo flag off for all normal dev/build. Statically false → every
  // `if (__DEMO__)` branch and the whole src/demo graph tree-shakes away.
  define: { __DEMO__: 'false' },
  plugins: [react(), tailwindcss()],
  server: {
    // Pin the port: localStorage (where the canvas template lives) is keyed by
    // origin, so a drifting port silently orphans the saved draft. strictPort
    // fails loudly on a stale instance instead of bumping to 5174/5175/...
    port: 5173,
    strictPort: true,
    proxy: {
      // Use IPv4 explicitly: `localhost` may resolve to IPv6 first and collide
      // with other services (e.g. Docker Desktop binding * on :8000).
      '/api': {
        target: API_PROXY_TARGET,
        // Object form, not the `'/api': target` shorthand — Vite normalizes
        // that shorthand to `changeOrigin: true`, which rewrites `Host` to the
        // proxy target (`127.0.0.1:8000`) while the browser's `Origin` stays
        // `http://localhost:5173`. The backend's cross-origin write guard
        // (`openpoly/api/security.py`) compares the browser's `Origin` against
        // the forwarded `Host`, so a rewritten Host makes every proxied
        // mutation look cross-origin. Browsers that send `Sec-Fetch-Site:
        // same-origin` are settled before that comparison, but any client
        // without fetch metadata (older Safari/Firefox, a metadata-stripping
        // intermediary) would 403 on every canvas save. Forwarding the
        // browser's own Host keeps proxied requests same-authority either way.
        changeOrigin: false,
      },
    },
  },
})
