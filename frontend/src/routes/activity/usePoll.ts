/**
 * Mount-scoped polling hook for the Activity tabs.
 *
 * The initial fetch runs unconditionally; only the interval is gated on tab
 * visibility, so a background tab does not keep hitting the API but also never
 * hangs forever on "Loading…". The fetcher is held in a ref so passing an
 * inline arrow does not re-subscribe the effect.
 *
 * Runs are chained with setTimeout: the next fetch is armed only after the
 * previous one settles, so an endpoint slower than the interval never stacks
 * requests on the backend (it runs sync handlers in a bounded threadpool, and
 * a pile-up there starves every other route).
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export type PollStatus = 'loading' | 'ready' | 'error'

export type PollResult<T> = {
  data: T | null
  status: PollStatus
  error: string | null
}

export type Poller = {
  /** Run now (unless a run is in flight) and restart the interval. `force`
   * ignores `shouldRun` — used for the initial fetch. */
  kick: (opts?: { force?: boolean }) => void
  stop: () => void
}

/**
 * Poll `task` every `intervalMs` measured from the end of the previous
 * run, never overlapping. Timed runs are skipped while `shouldRun()` is
 * false; the timer stays armed so polling resumes on its own.
 *
 * Each run gets an `AbortSignal` that fires after `timeoutMs` or on `stop()`.
 * A run that never settles is abandoned at the timeout, so a hung request
 * cannot stop polling for good.
 */
export function schedulePoll(
  task: (signal: AbortSignal) => Promise<void>,
  intervalMs: number,
  shouldRun: () => boolean,
  timeoutMs = Math.max(30_000, intervalMs),
): Poller {
  let timer: ReturnType<typeof setTimeout> | undefined
  let current: AbortController | undefined
  let stopped = false

  const arm = () => {
    clearTimeout(timer)
    if (!stopped) timer = setTimeout(() => run(false), intervalMs)
  }
  const run = (force: boolean) => {
    if (stopped || current) return
    if (!force && !shouldRun()) {
      arm()
      return
    }
    clearTimeout(timer)
    const controller = new AbortController()
    current = controller
    // fetch rejects with the abort reason, so the UI shows this, not a bare
    // "signal is aborted without reason".
    const deadline = setTimeout(
      () => controller.abort(new Error(`Request timed out after ${timeoutMs / 1000}s`)),
      timeoutMs,
    )
    const aborted = new Promise<void>((resolve) => {
      controller.signal.addEventListener('abort', () => resolve(), { once: true })
    })
    Promise.race([Promise.resolve().then(() => task(controller.signal)), aborted])
      .catch(() => {})
      .finally(() => {
        clearTimeout(deadline)
        if (current === controller) current = undefined
        arm()
      })
  }

  return {
    kick: (opts) => run(opts?.force ?? false),
    stop: () => {
      stopped = true
      clearTimeout(timer)
      current?.abort()
    },
  }
}

export function usePoll<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs = 3000,
): PollResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [status, setStatus] = useState<PollStatus>('loading')
  const [error, setError] = useState<string | null>(null)
  const fetcherRef = useRef(fetcher)
  useLayoutEffect(() => {
    fetcherRef.current = fetcher
  })

  useEffect(() => {
    let cancelled = false
    // Only the latest run may write state: a run abandoned at its timeout can
    // still settle later if its fetcher ignores the signal.
    let latestRun = 0
    async function refresh(signal: AbortSignal) {
      const run = ++latestRun
      const stale = () => cancelled || run !== latestRun
      try {
        const result = await fetcherRef.current(signal)
        if (stale()) return
        setData(result)
        setStatus('ready')
        setError(null)
      } catch (e) {
        if (stale()) return
        setStatus('error')
        setError(e instanceof Error ? e.message : String(e))
      }
    }
    const isVisible = () => document.visibilityState === 'visible'
    const poller = schedulePoll(refresh, intervalMs, isVisible)
    poller.kick({ force: true })
    const onVisibilityChange = () => {
      if (isVisible()) poller.kick()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      cancelled = true
      poller.stop()
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [intervalMs])

  return { data, status, error }
}
