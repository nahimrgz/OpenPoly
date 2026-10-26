/** Client + types for GET /api/portfolio/equity. */

export type EquityPoint = {
  ts: number
  equity: number
  realized: number
  unrealized: number
}

export type EquitySummary = {
  realized: number
  unrealized: number
  total: number
  open_positions: number
}

export type EquityResponse = {
  points: EquityPoint[]
  summary: EquitySummary
}

export async function fetchEquity(signal?: AbortSignal): Promise<EquityResponse> {
  const r = await fetch('/api/portfolio/equity', { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as EquityResponse
}
