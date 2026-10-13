import { describe, expect, it } from 'vitest'

import {
  callKey,
  dedupeCalls,
  formatCost,
  formatTokens,
  formatUsageWindow,
  INITIAL_REASONING,
  reasoningText,
  settleReasoningRequest,
  startReasoningRequest,
  TRUNCATION_MARKER,
} from './callsView'

describe('call identity', () => {
  it('keys a call by ts + news_id, independent of its position', () => {
    expect(callKey({ ts: 12.5, news_id: 'n1' })).toBe('12.5-n1')
  })

  it('dedupes repeated calls', () => {
    const a = { ts: 1, news_id: 'n1', v: 'first' }
    const b = { ts: 1, news_id: 'n1', v: 'dup' }
    const c = { ts: 2, news_id: 'n1', v: 'other call' }
    expect(dedupeCalls([a, b, c])).toEqual([a, c])
  })
})

describe('reasoning fetch state', () => {
  it('a late failure does not overwrite loaded text', () => {
    let s = startReasoningRequest(INITIAL_REASONING) // request 1
    const first = s.requestId
    s = startReasoningRequest(s) // request 2 (re-opened)
    s = settleReasoningRequest(s, s.requestId, { ok: true, text: 'Q1 …' })
    s = settleReasoningRequest(s, first, { ok: false })
    expect(s.text).toBe('Q1 …')
    expect(s.failed).toBe(false)
  })

  it('a stale failure is ignored, the newest failure is shown', () => {
    let s = startReasoningRequest(INITIAL_REASONING)
    const first = s.requestId
    s = startReasoningRequest(s)
    expect(settleReasoningRequest(s, first, { ok: false }).failed).toBe(false)
    expect(settleReasoningRequest(s, s.requestId, { ok: false }).failed).toBe(true)
  })

  it('marks truncated reasoning visibly', () => {
    expect(reasoningText('abc', true)).toBe(`abc${TRUNCATION_MARKER}`)
    expect(reasoningText('abc', false)).toBe('abc')
  })
})

describe('usage formatting', () => {
  it('formats tokens and cost', () => {
    expect(formatTokens(1234, 56)).toBe('1,234 in · 56 out')
    expect(formatTokens(null, null)).toBeNull()
    expect(formatCost(0.00042)).toBe('$0.00042')
    expect(formatCost(1.5)).toBe('$1.50')
    expect(formatCost(null)).toBeNull()
  })

  it('formats a totals window', () => {
    expect(
      formatUsageWindow('Today', {
        calls: 2,
        input_tokens: 2000,
        output_tokens: 300,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        cost_usd: 0.0035,
        unpriced_calls: 1,
      }),
    ).toBe('Today 2 calls · 2,000 in · 300 out · $0.00350 (+1 unpriced)')
  })
})
