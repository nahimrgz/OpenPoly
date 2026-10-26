import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchEquity } from './equityClient'
import { fetchNewsPipeline } from './newsClient'
import { fetchOrderBookHistory } from './orderBookClient'
import { fetchPosition, fetchPositionsData } from './positionsClient'
import { fetchWalletBalance } from './walletClient'

// usePoll aborts timed-out and unmounted runs through this signal; a client
// that drops it leaves the request running on the backend.
describe('activity clients forward the AbortSignal to fetch', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it.each([
    ['fetchEquity', (s: AbortSignal) => fetchEquity(s)],
    ['fetchWalletBalance', (s: AbortSignal) => fetchWalletBalance(s)],
    ['fetchOrderBookHistory', (s: AbortSignal) => fetchOrderBookHistory('t1', 0, null, s)],
    ['fetchNewsPipeline', (s: AbortSignal) => fetchNewsPipeline(10, s)],
    ['fetchPositionsData', (s: AbortSignal) => fetchPositionsData(s)],
    ['fetchPosition', (s: AbortSignal) => fetchPosition('p1', s)],
  ])('%s', async (_name, call) => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const { signal } = new AbortController()

    await call(signal).catch(() => {})

    expect(fetchMock).toHaveBeenCalled()
    for (const [, init] of fetchMock.mock.calls as unknown as [string, RequestInit][]) {
      expect(init.signal).toBe(signal)
    }
  })
})
