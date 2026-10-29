import { describe, expect, it } from 'vitest'

import { formatSentiment } from './sentiment'

describe('formatSentiment', () => {
  it('shows the upstream label as-is (tradingnews sends "neutral" / "positive")', () => {
    expect(formatSentiment('negative')).toBe('negative')
  })
  it('formats a numeric score to two decimals', () => {
    expect(formatSentiment(0.4)).toBe('0.40')
  })
  it('renders a missing value as a dash', () => {
    expect(formatSentiment(null)).toBe('—')
    expect(formatSentiment(undefined)).toBe('—')
  })
})
