/** Clients for the position ledger endpoints used by the Positions views. */
import type { Fill, PositionRecord } from './portfolioTypes'

export type PositionsData = { positions: PositionRecord[]; fills: Fill[] }

async function fetchJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as T
}

export async function fetchPositionsData(signal?: AbortSignal): Promise<PositionsData> {
  const [p, f] = await Promise.all([
    fetchJSON<{ positions: PositionRecord[] }>('/api/positions', signal),
    fetchJSON<{ fills: Fill[] }>('/api/fills', signal),
  ])
  return { positions: p.positions, fills: f.fills }
}

export async function fetchPosition(
  id: string,
  signal?: AbortSignal,
): Promise<PositionRecord | null> {
  const r = await fetch(`/api/positions/${encodeURIComponent(id)}`, { signal })
  if (r.status === 404) return null
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as PositionRecord
}
