import { describe, expect, it } from 'vitest'

import { defaultConfigForType, findEntry, MOCK_RUNTIME_CATALOG } from './catalog'
import type { RuntimeCatalogEntry } from './types'

function entry(
  name: string,
  over: Partial<RuntimeCatalogEntry> = {},
): RuntimeCatalogEntry {
  return {
    type: 'analyzer',
    name,
    version: '0.0.1',
    module: `m.${name}`,
    requires: [],
    source: 'builtin',
    param_schema: {},
    ...over,
  }
}

describe('findEntry', () => {
  it('prefers the entry the backend marks active', () => {
    const catalog = [entry('V0', { active: false }), entry('V1', { active: true })]
    expect(findEntry('analyzer', catalog)?.name).toBe('V1')
  })

  it('falls back to the first entry when no entry is marked active', () => {
    const catalog = [entry('V0'), entry('V1')]
    expect(findEntry('analyzer', catalog)?.name).toBe('V0')
  })

  it('returns undefined for an unknown type', () => {
    expect(findEntry('exit', [entry('V0')])).toBeUndefined()
  })
})

describe('MOCK_RUNTIME_CATALOG', () => {
  it('runs the v1 analyzer / entry / exit while keeping v0 registered', () => {
    expect(findEntry('analyzer', MOCK_RUNTIME_CATALOG)?.name).toBe('LLMAnalyzerV1')
    expect(findEntry('entry', MOCK_RUNTIME_CATALOG)?.name).toBe('EdgeThresholdEntryV1')
    expect(findEntry('exit', MOCK_RUNTIME_CATALOG)?.name).toBe('ThresholdExitV1')
    const names = MOCK_RUNTIME_CATALOG.map((e) => e.name)
    expect(names).toEqual(
      expect.arrayContaining([
        'LLMAnalyzerV0',
        'EdgeThresholdEntryV0',
        'ThresholdExitV0',
      ]),
    )
  })

  it('has exactly one active entry per section type', () => {
    const active = MOCK_RUNTIME_CATALOG.filter((e) => e.active).map((e) => e.type)
    expect(new Set(active).size).toBe(active.length)
    expect(new Set(active)).toEqual(new Set(MOCK_RUNTIME_CATALOG.map((e) => e.type)))
  })

  it('seeds a new entry node from the v1 defaults', () => {
    const cfg = defaultConfigForType('entry', MOCK_RUNTIME_CATALOG)
    expect(cfg.same_market_cooldown_minutes).toBe(60)
    expect(cfg.max_spread_pct).toBe(0.1)
    expect(cfg.block_opposite_side).toBe(true)
    // Every v0 knob is still there.
    expect(cfg.min_edge).toBe(0.05)
    expect(cfg.kill_max_drawdown_usd).toBe(0)
  })

  it('seeds a new exit node with the v1 hard stop', () => {
    const cfg = defaultConfigForType('exit', MOCK_RUNTIME_CATALOG)
    expect(cfg.hard_stop_loss_pct).toBe(0.3)
    expect(cfg.stop_loss_pct).toBe(0.15)
  })
})
