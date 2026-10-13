/**
 * Pure helpers behind the analyzer Calls tab — kept out of the component so
 * they are unit-testable without a DOM.
 */
import type { AnalyzerLogEntry, AnalyzerUsageWindow } from './logStore'

/**
 * Stable identity of one analyzer call: its timestamp plus news id. Never the
 * list index — the polled list grows at the front every 3s, so an
 * index-based key remounted every row and collapsed any open reasoning.
 */
export function callKey(e: Pick<AnalyzerLogEntry, 'ts' | 'news_id'>): string {
  return `${e.ts}-${e.news_id}`
}

/** Drops repeated calls (same key), keeping the first occurrence. */
export function dedupeCalls<T extends Pick<AnalyzerLogEntry, 'ts' | 'news_id'>>(
  entries: T[],
): T[] {
  const seen = new Set<string>()
  return entries.filter((e) => {
    const key = callKey(e)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/** Shown after a reasoning the backend cut at its 2000-character cap. */
export const TRUNCATION_MARKER = '… (truncated)'

export function reasoningText(text: string, truncated: boolean): string {
  return truncated ? `${text}${TRUNCATION_MARKER}` : text
}

/**
 * State of one call's on-demand reasoning fetch. ``requestId`` is the newest
 * request; a response for an older one is ignored, and a failure never
 * replaces text that is already loaded.
 */
export type ReasoningState = {
  text: string | null
  failed: boolean
  requestId: number
}

export const INITIAL_REASONING: ReasoningState = { text: null, failed: false, requestId: 0 }

export function startReasoningRequest(s: ReasoningState): ReasoningState {
  return { ...s, failed: false, requestId: s.requestId + 1 }
}

export type ReasoningOutcome = { ok: true; text: string } | { ok: false }

export function settleReasoningRequest(
  s: ReasoningState,
  requestId: number,
  outcome: ReasoningOutcome,
): ReasoningState {
  if (outcome.ok) {
    // Text wins whenever it arrives — even a late one for an older request
    // is the same call's reasoning.
    return { ...s, text: outcome.text, failed: false }
  }
  // A failure only counts for the newest request and only while nothing has
  // loaded: a late failed retry must not overwrite text already shown.
  if (requestId !== s.requestId || s.text !== null) return s
  return { ...s, failed: true }
}

/** "1,234 in · 56 out" (output includes thinking). */
export function formatTokens(input: number | null | undefined, output: number | null | undefined): string | null {
  if (input == null && output == null) return null
  return `${(input ?? 0).toLocaleString('en-US')} in · ${(output ?? 0).toLocaleString('en-US')} out`
}

/** USD with enough precision for sub-cent per-call costs. */
export function formatCost(usd: number | null | undefined): string | null {
  if (usd == null) return null
  if (usd === 0) return '$0'
  if (usd < 0.01) return `$${usd.toFixed(5)}`
  return `$${usd.toFixed(2)}`
}

/** One totals line, e.g. "Today 12 calls · 34,000 in · 1,200 out · $0.0140". */
export function formatUsageWindow(label: string, w: AnalyzerUsageWindow): string {
  const cost = w.calls === 0 ? '$0' : (formatCost(w.cost_usd) ?? '$0')
  const unpriced = w.unpriced_calls > 0 ? ` (+${w.unpriced_calls} unpriced)` : ''
  return `${label} ${w.calls} calls · ${formatTokens(w.input_tokens, w.output_tokens)} · ${cost}${unpriced}`
}
