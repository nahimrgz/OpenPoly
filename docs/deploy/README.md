# Deployment

openPoly has two deployment shapes. **Pick based on where you are**, not on
scale — it's a single-process backend + a SQLite file + a frontend either way.

| | Default — same machine | Separated — geoblock workaround |
|---|---|---|
| **When** | Development, paper trading, or live trading from a region Polymarket allows | Live trading when your location is geoblocked by Polymarket |
| **Topology** | Backend + frontend on one box | Backend on a VPS in an allowed region; frontend on your laptop via SSH tunnel |
| **Setup** | Two commands (below) | See [`separated-deployment.md`](./separated-deployment.md) |

## Default — same machine

This is the path for almost everyone. Backend and frontend run on the same
machine; the frontend dev server proxies API calls to the local backend.

```bash
# 1. Backend — binds 127.0.0.1:8000 (uvicorn default), paper mode by default
uv run uvicorn openpoly.api.main:app

# 2. Frontend — in another terminal, proxies to the local backend on :8000
cd frontend && yarn install && yarn dev
```

That's it. The frontend's proxy target defaults to `http://127.0.0.1:8000`, so
no environment variable is needed when both run locally. Open the printed Vite
URL and the strategy canvas loads.

openPoly **defaults to paper mode** — no real funds are touched until you
explicitly switch to live (`POST /api/system/mode`). See the repository
[DISCLAIMER](../../DISCLAIMER.md) before going live.

## Securing the API

The backend binds `127.0.0.1`, and until Phase 3 that was the whole of its
access control. It is thinner than it sounds: `POST /api/system/mode` flips
paper→live, `PUT /api/wallet/config` repoints the signing key, and
`POST /api/secrets/local` writes the secret store — all reachable by anything
that can open a socket to the loopback port, which includes every other process
on the host and anything sharing your SSH tunnel.

Three guards now sit in front of that, answering different questions.

### `OPENPOLY_API_TOKEN` — who is calling

A shared secret sent as the `X-OpenPoly-Token` header. It is **required on
every mutating route** (POST / PUT / DELETE / PATCH) and ignored on reads,
which expose no secret values.

```bash
# Generate one, then put it in the backend's environment (.env / systemd unit)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
curl -X POST http://127.0.0.1:8000/api/system/mode \
  -H "X-OpenPoly-Token: $OPENPOLY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"live"}'
```

The value may be the literal token or a `*_ref` in the same indirection every
other secret uses (`env:NAME`, `local:name` — see
[`03-system-config.md`](../architecture/03-system-config.md)), so it need not
live in the unit file in plaintext. A ref that stops resolving **fails closed**:
every mutating route answers `401 invalid_api_token` rather than quietly
reverting to open.

| `OPENPOLY_API_TOKEN` | Mutating routes | Reads | Live mode |
|---|---|---|---|
| unset | open (one WARNING at startup) | open | **refused** — 403 `api_token_required` |
| set | require a matching header, else 401 | open | allowed (subject to the wallet preflight) |

Leaving it unset keeps the local development loop exactly as it was. It cannot
be left unset for live trading: real funds behind an unauthenticated endpoint is
not a state anyone should reach by omission, so the switch is refused outright.

> **Use ASCII characters only.** An HTTP header value cannot carry a code point
> above `U+00FF`, and the browser's `fetch` throws a `TypeError` rather than
> sending one — so a token with an accent or an emoji in it turns *every*
> mutating request from the web UI into a client-side crash, not a 401. The
> backend compares UTF-8 bytes and would accept such a token from `curl`, which
> makes the failure look like a UI bug rather than a token you cannot type. Keep
> it to printable ASCII; `secrets.token_urlsafe` above already does.

**Setting it in the web UI.** Open **Keys → API token** and paste the same value
you gave the backend. It is stored in that browser's `localStorage` (key
`openpoly_api_token`) and attached as `X-OpenPoly-Token` to mutating requests
only — reads keep the shape they always had. It lives in the browser rather than
in the backend secret store because it is the credential *for* that store, so it
cannot be kept behind it. Each browser needs its own copy; clearing site data
clears it.

### `OPENPOLY_ALLOWED_HOSTS` — what name they used

A browser can be induced to send requests to a loopback service, but it cannot
forge the `Host` header. Every request whose Host is neither loopback
(`localhost`, `127.0.0.1`, `[::1]`) nor listed here is answered
**421 Misdirected Request** — which is what turns the DNS-rebinding path into a
rejection instead of a live-mode switch.

```bash
# Only needed when the backend is reached through a real name (reverse proxy).
OPENPOLY_ALLOWED_HOSTS=openpoly.internal.example.com
```

The default loopback and SSH-tunnel setups need no entry: Vite's proxy is
configured with `changeOrigin: false` (`frontend/vite.config.ts`), so it
forwards the browser's own `Host` — `localhost:5173` or `127.0.0.1:5173` —
rather than rewriting it to the proxy target. That is deliberate, and the
`Origin` section below says why. If you
run the dev server with `--host` and open the UI from another machine on the
LAN, that machine's URL becomes the Host and you must add it here. `*` disables
the check; make that a deliberate choice, not a default. The `Host` check
matches on hostname alone, so an entry that pins a port (`host:port`, see the
`Origin` section below) counts only for the `Origin` check — list the bare
hostname as well if the backend is also *reached* under that name. There is deliberately **no
CORS allowance** — the frontend is same-origin through Vite's proxy, and a
permissive `Access-Control-Allow-Origin` would hand back exactly what the Host
allowlist takes away.

### Origin / `Sec-Fetch-Site` — whose page issued this

The Host allowlist admits loopback by design, and a body-less `POST` is a CORS
*simple request*: the browser sends it and only refuses the caller sight of the
**response**. So this, from any page you happen to have open —

```js
fetch('http://127.0.0.1:8000/api/positions/close-all', { method: 'POST' })
```

— used to reach the route and bulk-close the book. The attacker never saw the
answer, which does not undo the sell. The token is no defence either: a browser
attaches no header it was not asked to, and with the token unset (the loopback
default) nothing was being checked at all.

Every **mutating** request that does not come from this backend's own origin is
therefore refused with **403 `cross_origin_write`**, before it reaches any
route:

- `Sec-Fetch-Site: cross-site` → refused outright. The browser sets this header
  and page script cannot. The `Origin` is not consulted, so the allowlist below
  cannot undo this refusal — the 403 body says so.
- `Sec-Fetch-Site: same-origin` or `none` (a typed-in URL) → allowed. Both name
  this very origin.
- `Sec-Fetch-Site: same-site`, any value not listed above, or no fetch metadata
  at all → decided by the `Origin`. **`same-site` is not `same-origin`**: it
  only means the registrable domain matches, and every `localhost:<port>` is
  same-site with every other one, so a page served by any dev server or local
  tool on `localhost:3000` would otherwise be able to drive the backend on
  `localhost:8000` with the token unset.
- An `Origin` is allowed when it names the **same authority — host *and* port**
  — as the request's own `Host` (a scheme's default port is implied on both
  sides, so `https://host` matches `Host: host`), or when the operator listed
  it in `OPENPOLY_ALLOWED_HOSTS`. Anything else is refused, including
  `Origin: null` (sandboxed iframe, `data:` document), which counts as refused
  rather than absent. Loopback gets no free pass here: it is the one authority
  every local page shares.
- **Neither header present → allowed.** `curl`, a systemd timer and a Python
  client carry no hostile page's authority, and the guard exists to stop a
  browser being used as a confused deputy. Scripted use is unaffected.

Reads are not guarded: a cross-origin `GET` leaks nothing the browser will hand
back anyway.

`OPENPOLY_ALLOWED_HOSTS` is the escape hatch for a UI served from somewhere
else. An entry may be a bare host (`openpoly.internal.example.com` — admits it
as an `Origin` on any port) or pin the port (`openpoly.internal.example.com:5173`,
`localhost:3000` — admits that origin only). The default Vite setup needs no
entry: the proxy forwards the browser's own `Host` (`changeOrigin: false` in
`frontend/vite.config.ts`), so a canvas save arrives with `Origin:
http://localhost:5173` against `Host: localhost:5173` and is same-authority on
the `Origin` check alone. Vite's `'/api': target` shorthand would instead
normalize to `changeOrigin: true` and rewrite the `Host` to the proxy target,
which only stays working for browsers that send `Sec-Fetch-Site: same-origin`
— a client without fetch metadata would be 403'd on every mutation. If you put
your own reverse proxy in front of the backend, either preserve the browser's
`Host` the same way or add the UI's origin to `OPENPOLY_ALLOWED_HOSTS`.

## Disk growth

`order_book_snapshot` is the one table that grows without bound: one row per
tracked token per sampling cycle, forever. The database section prunes it
hourly, keeping the last **7 days** by default (`order_book_retention_days` on
its config; `0` disables the prune). Keep the window longer than your longest
held position — peak bootstrap rebuilds a trailing stop from the snapshots
taken since the position opened. How much has been reclaimed shows up as
`retention.pruned_rows` on `GET /api/inspect/db-status`.

Schema changes are applied by a small versioned migration runner
(`openpoly/db/migrations.py`) recording its progress in a `schema_version`
table, so an existing database is upgraded in place on startup and an
interrupted migration is retried rather than half-applied.

## Why a separated mode exists at all

Polymarket's CLOB `POST /order` endpoint is **region-blocked**. Order placement
from a geoblocked location (the US and ~32 other countries — see
[Polymarket's geoblock docs](https://docs.polymarket.com/developers/CLOB/geoblock))
returns:

```
403 "Trading restricted in your region"
```

Reads (markets, prices, your positions) work from anywhere; only **order
submission** is blocked. So if you're in a blocked region and want to trade
live, the backend — the part that submits orders — has to run from an allowed
region. The frontend can stay on your laptop and reach it over an SSH tunnel.

That's the only reason for the separated topology. If you're not geoblocked,
ignore it entirely and use the same-machine setup above.

> Determining whether trading on Polymarket is legal where you are is **your
> responsibility** — see the [DISCLAIMER](../../DISCLAIMER.md). openPoly does
> not endorse circumventing any legal restriction.

For the separated setup (VPS, systemd, SSH tunnel, CPU-only install notes), see
[`separated-deployment.md`](./separated-deployment.md).
