# openPoly frontend

React + React Flow canvas UI for the openPoly strategy pipeline. Built with Vite
+ TypeScript.

For the full picture — what openPoly is, how to run the backend, and the
default (same-machine) vs separated deployment models — see the
[repository README](../README.md).

## Local dev

The frontend is a Vite dev server that proxies API calls to the backend.

```bash
# from frontend/
yarn install
yarn dev
```

The dev server proxies `/api` to `http://127.0.0.1:8000` by default, so no
environment variable is needed when the backend runs on the same machine.
`VITE_API_PROXY_TARGET` overrides the target — e.g.
`VITE_API_PROXY_TARGET=http://127.0.0.1:18000 yarn dev` for the geoblock /
separated-deployment SSH tunnel. See [`docs/deploy/`](../docs/deploy/) for both.

## Scripts

| Command | What it does |
|---|---|
| `yarn dev` | Start the Vite dev server (HMR) |
| `yarn build` | Type-check (`tsc -b`) + production build |
| `yarn typecheck` | Type-check only, no emit |
| `yarn lint` | ESLint |
| `yarn test` | Vitest unit suite (`vitest run`) |
| `yarn format` | Prettier write |

Tests live next to the module they cover (`src/canvas/store.test.ts`). Vitest
runs in the `node` environment — no jsdom — so the suite covers logic modules
(state transitions, (de)serialization, the API client) rather than rendering.
`src/testing/` holds the shared stubs, e.g. the in-memory `localStorage`.

## Layout

`src/sections/` mirrors the backend `openpoly/sections/` by name — each strategy
section's canvas node lives alongside its backend impl's namesake folder.
