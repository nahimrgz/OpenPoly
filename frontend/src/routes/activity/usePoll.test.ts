import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { schedulePoll } from './usePoll'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

describe('schedulePoll', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('waits a full interval after each run finishes, never overlapping', async () => {
    const pending = deferred()
    let calls = 0
    const poll = schedulePoll(
      () => {
        calls += 1
        return calls === 1 ? pending.promise : Promise.resolve()
      },
      1000,
      () => true,
    )

    poll.kick()
    await vi.advanceTimersByTimeAsync(5000) // first run is still pending
    expect(calls).toBe(1)

    pending.resolve()
    await vi.advanceTimersByTimeAsync(999)
    expect(calls).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toBe(2)
    poll.stop()
  })

  it('kick runs now and restarts the interval instead of adding a run', async () => {
    let calls = 0
    const poll = schedulePoll(
      () => {
        calls += 1
        return Promise.resolve()
      },
      1000,
      () => true,
    )
    poll.kick()
    await vi.advanceTimersByTimeAsync(600)
    poll.kick() // e.g. the tab became visible again
    await vi.advanceTimersByTimeAsync(600)
    expect(calls).toBe(2)
    await vi.advanceTimersByTimeAsync(400)
    expect(calls).toBe(3)
    poll.stop()
  })

  it('skips timed runs while hidden but keeps the timer armed', async () => {
    let visible = false
    let calls = 0
    const poll = schedulePoll(
      () => {
        calls += 1
        return Promise.resolve()
      },
      1000,
      () => visible,
    )
    poll.kick({ force: true }) // the initial fetch ignores visibility
    await vi.advanceTimersByTimeAsync(3000)
    expect(calls).toBe(1)
    visible = true
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toBe(2)
    poll.stop()
  })

  it('keeps polling after a failed run and stops for good on stop()', async () => {
    let calls = 0
    const poll = schedulePoll(
      () => {
        calls += 1
        return Promise.reject(new Error('boom'))
      },
      1000,
      () => true,
    )
    poll.kick()
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toBe(2)
    poll.stop()
    await vi.advanceTimersByTimeAsync(5000)
    expect(calls).toBe(2)
  })

  it('aborts a run that never settles and keeps polling', async () => {
    const signals: AbortSignal[] = []
    const poll = schedulePoll(
      (signal) => {
        signals.push(signal)
        return new Promise<void>(() => {}) // hung request
      },
      1000,
      () => true,
      5000,
    )
    poll.kick()
    await vi.advanceTimersByTimeAsync(4999)
    expect(signals).toHaveLength(1)
    expect(signals[0].aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(signals[0].aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(signals).toHaveLength(2)
    poll.stop()
  })

  it('does not lock up when the task throws synchronously', async () => {
    let calls = 0
    const poll = schedulePoll(
      () => {
        calls += 1
        throw new Error('sync boom')
      },
      1000,
      () => true,
    )
    poll.kick()
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toBe(2)
    poll.stop()
  })

  it('stop() aborts the run in flight', async () => {
    let seen: AbortSignal | undefined
    const poll = schedulePoll(
      (signal) => {
        seen = signal
        return new Promise<void>(() => {})
      },
      1000,
      () => true,
    )
    poll.kick()
    await vi.advanceTimersByTimeAsync(10)
    poll.stop()
    expect(seen?.aborted).toBe(true)
  })
})
