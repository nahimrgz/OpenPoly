/**
 * API token card — first card in the Keys drawer.
 *
 * When the backend runs with `OPENPOLY_API_TOKEN` set, every mutating route
 * (canvas save, source start/stop, wallet, mode switch, close-all, secrets)
 * requires the matching `X-OpenPoly-Token` header, and without it the UI gets a
 * 401 on every write while reads keep working — which looks like "the canvas
 * silently stopped saving" rather than like an auth problem. This is where the
 * operator pastes that token.
 *
 * It lives in the browser's `localStorage`, not in the backend secret store:
 * it is the credential *for* that store, so it cannot be kept behind it.
 */
import { useState } from 'react'

import {
  API_TOKEN_HEADER,
  getApiToken,
  isTokenTransportable,
  setApiToken,
} from '../lib/apiClient'
import { Card, GhostButton, PrimaryButton, inputCls, labelCls } from './atoms'

export function ApiTokenPanel() {
  const [token, setToken] = useState(() => getApiToken())
  const [saved, setSaved] = useState<string | null>(null)

  const trimmed = token.trim()
  const nonAscii = trimmed !== '' && !isTokenTransportable(trimmed)
  const stored = getApiToken()

  function onSave() {
    setApiToken(token)
    const persisted = getApiToken()
    setToken(persisted)
    if (trimmed && persisted !== trimmed) {
      // setItem threw (private mode, storage blocked): setApiToken swallows
      // the error, so the re-read is the only honest signal.
      setSaved('Token could not be stored in this browser.')
    } else {
      setSaved(trimmed ? 'Token saved for this browser.' : 'Token cleared.')
    }
  }

  function onClear() {
    setApiToken('')
    setToken('')
    setSaved('Token cleared.')
  }

  return (
    <Card
      title="API token"
      action={
        <GhostButton onClick={onClear} variant="danger" disabled={stored === ''}>
          Clear
        </GhostButton>
      }
    >
      <div className="flex flex-col gap-3">
        <label className={labelCls}>
          <span>
            Shared secret sent as <code>{API_TOKEN_HEADER}</code>
          </span>
          <input
            type="password"
            autoComplete="off"
            className={inputCls}
            value={token}
            placeholder="(backend running without OPENPOLY_API_TOKEN — leave empty)"
            onChange={(e) => {
              setToken(e.target.value)
              setSaved(null)
            }}
          />
          <span className="text-[11px] text-neutral-500">
            Must equal the backend&apos;s <code>OPENPOLY_API_TOKEN</code>. Stored in this
            browser only, and attached to mutating requests only — reads are
            unauthenticated by design. ASCII characters only: an HTTP header cannot carry
            anything else.
          </span>
        </label>

        {nonAscii && (
          <div className="text-xs text-red-300 break-words">
            This token contains non-ASCII characters and cannot be sent as an HTTP header.
            Use an ASCII-only secret on the backend.
          </div>
        )}

        <div className="flex items-center justify-between gap-3">
          <span className="text-[11px] text-neutral-500">
            {stored === '' ? 'No token stored.' : 'Token stored.'}
          </span>
          <div className="flex items-center gap-2">
            {saved && <span className="text-[11px] text-neutral-400">{saved}</span>}
            <PrimaryButton onClick={onSave} disabled={nonAscii}>
              Save
            </PrimaryButton>
          </div>
        </div>
      </div>
    </Card>
  )
}
