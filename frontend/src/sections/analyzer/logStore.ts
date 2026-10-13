/**
 * Analyzer log store (v7 P5). Polls GET /api/analyzer/log every 3s while
 * the page is visible. Mirrors useNewsSourceStatusStore's pattern:
 * - status === 'loading' only on the cold first fetch (no polling flicker)
 * - keep last data on transient errors so the tab can still render
 * - visibilitychange pauses polling; immediate refresh on return
 * - HMR cleans up the interval so dev double-poll doesn't accumulate
 */
import { create } from 'zustand'

export type Verdict = 'ok' | 'skip' | 'fail_open' | 'error'

export type AnalyzerLogEntry = {
  ts: number
  news_id: string
  news_content_preview: string
  urgency: string
  verdict: Verdict
  p_model: number | null
  confidence: string | null
  market_id: string | null
  latency_ms: number
  error: string | null
  /** Whether the analyzer wrote a Q1-Q3 self-check (llm_v1); fetch it with
   * `fetchReasoning`. Absent on older backends. */
  has_reasoning?: boolean
  /** Why the runtime skipped the call (e.g. stale_news). */
  reason?: string | null
  /** LLM usage of the call, every attempt summed (llm_v1; absent on older
   * backends / analyzers). ``output_tokens`` include thinking. */
  llm_model?: string | null
  input_tokens?: number | null
  output_tokens?: number | null
  cache_read_input_tokens?: number | null
  cache_creation_input_tokens?: number | null
  llm_attempts?: number | null
  /** Estimated USD; null for a model without a known price. */
  cost_usd?: number | null
}

export type AnalyzerUsageWindow = {
  calls: number
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  cost_usd: number
  unpriced_calls: number
}

export type AnalyzerUsageTotals = {
  today: AnalyzerUsageWindow
  last_7_days: AnalyzerUsageWindow
  all_time: AnalyzerUsageWindow
}

export type AnalyzerLogResponse = {
  entries: AnalyzerLogEntry[]
  counters: Record<Verdict, number>
  last_at: number | null
  queue_depth: number
  state: 'stopped' | 'running'
}

export type ReasoningResponse = { reasoning: string | null; truncated: boolean }

export type FetchStatus = 'idle' | 'loading' | 'ready' | 'error'

type StoreState = {
  data: AnalyzerLogResponse | null
  /** Usage totals (GET /api/analyzer/usage); null until loaded or on an
   * older backend. */
  usage: AnalyzerUsageTotals | null
  status: FetchStatus
  error: string | null
  refresh: () => Promise<void>
}

const ENDPOINT = '/api/analyzer/log'

/**
 * One call's analyzer reasoning (left out of the polled list), looked up by
 * the call's unique (ts, news_id) — a news id alone can match several calls.
 */
export async function fetchReasoning(newsId: string, ts: number): Promise<ReasoningResponse> {
  const r = await fetch(
    `${ENDPOINT}/${encodeURIComponent(newsId)}/reasoning?ts=${encodeURIComponent(String(ts))}`,
  )
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  const body = (await r.json()) as { reasoning: string | null; truncated?: boolean }
  return { reasoning: body.reasoning, truncated: body.truncated === true }
}

const USAGE_ENDPOINT = '/api/analyzer/usage'

async function fetchUsage(): Promise<AnalyzerUsageTotals | null> {
  try {
    const r = await fetch(USAGE_ENDPOINT)
    if (!r.ok) return null
    return (await r.json()) as AnalyzerUsageTotals
  } catch {
    return null
  }
}
const POLL_INTERVAL_MS = 3000

let inflight: Promise<void> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null

async function fetchLog(): Promise<AnalyzerLogResponse> {
  const r = await fetch(ENDPOINT)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as AnalyzerLogResponse
}

export const useAnalyzerLogStore = create<StoreState>((set, get) => ({
  data: null,
  usage: null,
  status: 'idle',
  error: null,
  refresh: async () => {
    if (inflight) return inflight
    inflight = (async () => {
      if (get().data === null) set({ status: 'loading' })
      try {
        const [body, usage] = await Promise.all([fetchLog(), fetchUsage()])
        set({ data: body, usage: usage ?? get().usage, status: 'ready', error: null })
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        set({ status: 'error', error: msg })
      } finally {
        inflight = null
      }
    })()
    return inflight
  },
}))

function isVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible'
}

if (typeof window !== 'undefined') {
  void useAnalyzerLogStore.getState().refresh()
  pollTimer = setInterval(() => {
    if (!isVisible()) return
    void useAnalyzerLogStore.getState().refresh()
  }, POLL_INTERVAL_MS)
  document.addEventListener('visibilitychange', () => {
    if (isVisible()) void useAnalyzerLogStore.getState().refresh()
  })
  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      if (pollTimer !== null) clearInterval(pollTimer)
    })
  }
}
