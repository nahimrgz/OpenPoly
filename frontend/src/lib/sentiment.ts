/**
 * News sentiment as the backend sends it. The upstream feed defines it
 * free-form: tradingnews emits labels ("neutral" / "positive" /
 * "negative"), other sources may send a numeric score. The backend keeps it
 * as-is (``NewsItem.sentiment``), so the UI must accept either.
 */
export type Sentiment = string | number | null

export function formatSentiment(v: Sentiment | undefined): string {
  if (v === null || v === undefined) return '—'
  return typeof v === 'number' ? v.toFixed(2) : v
}
