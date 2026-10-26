import { describe, expect, it } from 'vitest'

import { singleFlight } from './usePoll'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

describe('singleFlight', () => {
  it('skips calls while the previous one is still pending', async () => {
    const pending = deferred()
    let calls = 0
    const tick = singleFlight(() => {
      calls += 1
      return pending.promise
    })

    tick()
    tick()
    tick()
    expect(calls).toBe(1)

    pending.resolve()
    await pending.promise
    await Promise.resolve()
    tick()
    expect(calls).toBe(2)
  })

  it('allows the next call after a rejection', async () => {
    let calls = 0
    const tick = singleFlight(() => {
      calls += 1
      return Promise.reject(new Error('boom'))
    })

    tick()
    await new Promise((r) => setTimeout(r, 0))
    tick()
    expect(calls).toBe(2)
  })
})
