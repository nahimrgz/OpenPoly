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
 */
export function schedulePoll(
  task: () => Promise<void>,
  intervalMs: number,
  shouldRun: () => boolean,
): Poller {
  let timer: ReturnType<typeof setTimeout> | undefined
  let running = false
  let stopped = false

  const arm = () => {
    clearTimeout(timer)
    if (!stopped) timer = setTimeout(() => run(false), intervalMs)
  }
  const run = (force: boolean) => {
    if (stopped || running) return
    if (!force && !shouldRun()) {
      arm()
      return
    }
    clearTimeout(timer)
    running = true
    task()
      .catch(() => {})
      .finally(() => {
        running = false
        arm()
      })
  }

  return {
    kick: (opts) => run(opts?.force ?? false),
    stop: () => {
      stopped = true
      clearTimeout(timer)
    },
  }
}

export function usePoll<T>(
  fetcher: () => Promise<T>,
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
    async function refresh() {
      try {
        const result = await fetcherRef.current()
        if (cancelled) return
        setData(result)
        setStatus('ready')
        setError(null)
      } catch (e) {
        if (cancelled) return
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
