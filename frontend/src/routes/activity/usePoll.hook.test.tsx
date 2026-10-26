// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePoll } from './usePoll'

let visibility: DocumentVisibilityState = 'visible'

function setVisibility(v: DocumentVisibilityState) {
  visibility = v
  document.dispatchEvent(new Event('visibilitychange'))
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('usePoll', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    visibility = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibility,
    })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('loads once on mount even while hidden, then waits until visible', async () => {
    visibility = 'hidden'
    let calls = 0
    const { result } = renderHook(() =>
      usePoll(async () => {
        calls += 1
        return calls
      }, 1000),
    )
    await advance(0)
    expect(result.current).toEqual({ data: 1, status: 'ready', error: null })

    await advance(5000)
    expect(calls).toBe(1)

    act(() => setVisibility('visible'))
    await advance(0)
    expect(calls).toBe(2)
    expect(result.current.data).toBe(2)
  })

  it('surfaces a timed-out request as an error and keeps the last data', async () => {
    let calls = 0
    const { result } = renderHook(() =>
      usePoll(
        (signal) => {
          calls += 1
          if (calls === 1) return Promise.resolve('first')
          return new Promise<string>((_, reject) => {
            signal.addEventListener('abort', () => reject(signal.reason as Error))
          })
        },
        1000,
      ),
    )
    await advance(0)
    expect(result.current.data).toBe('first')

    await advance(1000 + 30_000)
    expect(result.current).toEqual({
      data: 'first',
      status: 'error',
      error: 'Request timed out after 30s',
    })
  })

  it('aborts the request in flight on unmount and stops polling', async () => {
    const signals: AbortSignal[] = []
    const { unmount } = renderHook(() =>
      usePoll((signal) => {
        signals.push(signal)
        return new Promise<never>(() => {})
      }, 1000),
    )
    await advance(0)
    expect(signals).toHaveLength(1)

    unmount()
    expect(signals[0].aborted).toBe(true)
    await advance(60_000)
    expect(signals).toHaveLength(1)
  })

  it('ignores a late result from an abandoned run', async () => {
    let calls = 0
    let releaseStale!: (v: string) => void
    const { result } = renderHook(() =>
      usePoll(() => {
        calls += 1
        if (calls === 1) return Promise.resolve('first')
        // Ignores the abort signal, so it outlives its timeout.
        if (calls === 2) return new Promise<string>((r) => (releaseStale = r))
        return Promise.resolve('fresh')
      }, 1000),
    )
    await advance(0)
    await advance(1000 + 30_000 + 1000) // run 2 abandoned at the deadline, run 3 done
    expect(result.current.data).toBe('fresh')

    await act(async () => releaseStale('stale'))
    expect(result.current.data).toBe('fresh')
  })
})
