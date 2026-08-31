/**
 * Shared API token plumbing.
 *
 * The backend guards every mutating route (POST / PUT / DELETE / PATCH) with an
 * optional shared secret in the `X-OpenPoly-Token` header — see
 * `openpoly/api/security.py`. When `OPENPOLY_API_TOKEN` is unset the backend
 * runs open (loopback dev default) and everything here is a no-op; when it *is*
 * set, a UI that does not send the header gets a 401 on every write.
 *
 * The token is held in `localStorage` rather than in the canvas template or the
 * backend secret store: it is the credential *for* the backend, so it cannot
 * live behind the API it authenticates, and it is per-browser rather than
 * per-deployment. Reads are deliberately left unauthenticated by the backend,
 * so the header is attached to mutating requests only — a GET keeps exactly the
 * shape it had before.
 *
 * ASCII only. An HTTP header value cannot carry a code point above U+00FF, and
 * `fetch` throws a `TypeError` on one, which would turn "wrong token" into
 * "every write crashes". A non-ASCII token is therefore refused here (and by
 * the docs) rather than sent — see `docs/deploy/README.md`.
 */

export const API_TOKEN_STORAGE_KEY = 'openpoly_api_token'
export const API_TOKEN_HEADER = 'X-OpenPoly-Token'

const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** True when every code point fits in a byte, i.e. the value is header-safe. */
export function isTokenTransportable(token: string): boolean {
  return /^[\x20-\x7e]*$/.test(token)
}

/** The configured token, or `''` when none is set / storage is unavailable. */
export function getApiToken(): string {
  if (typeof localStorage === 'undefined') return ''
  try {
    return localStorage.getItem(API_TOKEN_STORAGE_KEY) ?? ''
  } catch {
    // Private-mode Safari and friends throw on access rather than returning
    // null. No token is a working state (open backend), so degrade quietly.
    return ''
  }
}

/** Store the token; a blank value clears it. Returns the stored value. */
export function setApiToken(token: string): string {
  const trimmed = token.trim()
  if (typeof localStorage === 'undefined') return trimmed
  try {
    if (trimmed) localStorage.setItem(API_TOKEN_STORAGE_KEY, trimmed)
    else localStorage.removeItem(API_TOKEN_STORAGE_KEY)
  } catch {
    /* ignore — the caller surfaces "not persisted" via a re-read */
  }
  return trimmed
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  // init wins per the fetch spec, but a Request input carries its own method:
  // an init-only check classified apiFetch(new Request(url, {method: 'POST'}))
  // as a GET and silently skipped the token header.
  if (init?.method) return init.method
  if (typeof Request !== 'undefined' && input instanceof Request) return input.method
  return 'GET'
}

function isMutating(input: RequestInfo | URL, init?: RequestInit): boolean {
  return MUTATING_METHODS.has(requestMethod(input, init).toUpperCase())
}

/**
 * `fetch` with the API token attached to mutating requests.
 *
 * Identical to `fetch` for reads, and identical to `fetch` for writes when no
 * token is configured — so wiring it in changes nothing for a loopback dev
 * backend. Callers keep their own error handling; this adds a header and
 * nothing else.
 */
export function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  if (!isMutating(input, init)) return fetch(input, init)
  const token = getApiToken()
  if (!token || !isTokenTransportable(token)) {
    if (token) {
      console.error(
        `${API_TOKEN_HEADER} not sent: the stored token contains non-ASCII ` +
          'characters, which an HTTP header cannot carry. Set an ASCII token.',
      )
    }
    return fetch(input, init)
  }
  // Seed from init.headers when given (it replaces a Request's headers per
  // the fetch spec), else from the Request input's own headers so they
  // survive the wrapper instead of being dropped by the spread below.
  const headers = new Headers(
    init?.headers ??
      (typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined),
  )
  headers.set(API_TOKEN_HEADER, token)
  return fetch(input, { ...init, headers })
}
