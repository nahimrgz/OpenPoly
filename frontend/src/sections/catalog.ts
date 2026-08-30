import type { ConfigValues, RuntimeCatalogEntry, SectionType } from './types'

export const TYPE_DISPLAY: Record<SectionType, { label: string; description: string }> = {
  news_source: {
    label: 'News source',
    description: 'External event stream feeding the Analyzer.',
  },
  market_source: {
    label: 'Market source',
    description: 'Polls Polymarket and filters the tradeable market catalog.',
  },
  embedding: {
    label: 'Embedding',
    description: 'Ranks the market catalog against news by semantic similarity.',
  },
  analyzer: {
    label: 'Analyzer',
    description: 'News → (market_id, p_model, confidence) via LLM.',
  },
  entry: {
    label: 'Entry',
    description: 'AnalysisResult → OrderIntent (open/add position) based on edge.',
  },
  exit: {
    label: 'Exit',
    description: 'Position → CloseIntent on take-profit / stop-loss thresholds.',
  },
  database: {
    label: 'Database',
    description: 'Persists order books + news to SQLite; inspect each table.',
  },
}

export const SECTION_ORDER: SectionType[] = [
  'news_source',
  'market_source',
  'embedding',
  'analyzer',
  'entry',
  'exit',
  'database',
]

export function defaultsFromSchema(schema: Record<string, unknown>): ConfigValues {
  const props = (schema.properties ?? {}) as Record<string, { default?: unknown }>
  const out: ConfigValues = {}
  for (const [key, val] of Object.entries(props)) {
    if (val && 'default' in val && val.default !== undefined) {
      out[key] = val.default as ConfigValues[string]
    }
  }
  return out
}

export function defaultConfigForType(
  type: SectionType,
  catalog: RuntimeCatalogEntry[],
): ConfigValues {
  const entry = catalog.find((e) => e.type === type)
  if (!entry) return {}
  return defaultsFromSchema(entry.param_schema)
}

export function findEntry(
  type: SectionType,
  catalog: RuntimeCatalogEntry[],
): RuntimeCatalogEntry | undefined {
  return catalog.find((e) => e.type === type)
}

/**
 * Offline fallback catalog. Mirrors what /api/sections/catalog returns from a
 * stock backend with the baseline section impls (news_source / market_source /
 * analyzer / entry / exit / database).
 *
 * Used when the backend is unreachable so the canvas remains operable. When
 * runtime catalog loads, the live data takes over via catalogStore.
 *
 * KEEP IN SYNC WITH THE BACKEND. Each entry mirrors one section's
 * `SECTION_VERSION` plus its pydantic `Config.model_json_schema()`. Drift is
 * silent and expensive: `defaultConfigForType` seeds a new node's config from
 * whatever is written here, so a field missing from this file is a field the
 * canvas never sets — the backend then falls back to its own default and the
 * operator's node claims a configuration it does not have. When a Config gains,
 * loses, or re-defaults a field, update the matching entry here and bump its
 * `version` to the section's new `SECTION_VERSION`.
 */
export const MOCK_RUNTIME_CATALOG: RuntimeCatalogEntry[] = [
  {
    type: 'news_source',
    name: 'TradingNewsWSSource',
    version: '0.1.0',
    module: 'openpoly.sections.news_source.tradingnews_ws',
    requires: [],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'TradingNewsWSConfig',
      properties: {
        endpoint: {
          type: 'string',
          title: 'Endpoint',
          description: 'WebSocket endpoint URL.',
          default: 'wss://api.tradingnews.press/v1/stream',
        },
        api_key_ref: {
          type: 'string',
          title: 'Api Key Ref',
          description: 'Reference to the API key (e.g. env:VAR_NAME).',
          default: 'env:OPENPOLY_TRADINGNEWS_KEY',
        },
        freshness_seconds: {
          type: 'integer',
          title: 'Freshness Seconds',
          description: 'Only forward news younger than this when Analyzer ticks.',
          default: 1800,
          minimum: 1,
          maximum: 86400,
        },
        urgency_filter: {
          type: 'string',
          title: 'Urgency Filter',
          description: 'Minimum urgency level to forward.',
          default: 'all',
          enum: ['all', 'low', 'medium', 'high'],
        },
        buffer_size: {
          type: 'integer',
          title: 'Buffer Size',
          description: 'Max in-memory news items retained.',
          default: 1000,
          minimum: 10,
          maximum: 100000,
        },
      },
    },
  },
  {
    type: 'market_source',
    name: 'PolymarketSource',
    version: '0.1.0',
    module: 'openpoly.sections.market_source.polymarket',
    requires: [],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'MarketSourceConfig',
      $defs: {
        MarketFilterConfig: {
          type: 'object',
          title: 'MarketFilterConfig',
          properties: {
            require_zero_fee: {
              type: 'boolean',
              title: 'Require Zero Fee',
              description: 'Drop markets with a non-zero taker fee (v8 zero-fee rule).',
              default: true,
            },
            min_hours_to_expiry: {
              type: 'number',
              title: 'Min Hours To Expiry',
              description: 'Drop markets resolving within this many hours.',
              default: 24.0,
              minimum: 0,
            },
            min_volume_24h: {
              type: 'number',
              title: 'Min Volume 24H',
              description: 'Minimum 24h USD volume.',
              default: 1000.0,
              minimum: 0,
            },
            min_liquidity: {
              type: 'number',
              title: 'Min Liquidity',
              description: 'Minimum liquidity (USD).',
              default: 500.0,
              minimum: 0,
            },
            min_price: {
              type: 'number',
              title: 'Min Price',
              description: 'Drop markets whose reference price is below this.',
              default: 0.03,
              minimum: 0,
              maximum: 0.5,
            },
            max_spread: {
              type: 'number',
              title: 'Max Spread',
              description: 'Drop markets with a wider spread.',
              default: 0.15,
              minimum: 0,
              maximum: 1,
            },
            exclude_event_tags: {
              type: 'array',
              title: 'Exclude Event Tags',
              description: 'Drop markets whose event carries any of these tag slugs.',
              default: ['sports'],
              items: { type: 'string' },
            },
          },
        },
      },
      properties: {
        poll_interval_seconds: {
          type: 'integer',
          title: 'Poll Interval Seconds',
          description: 'Seconds between discovery polls.',
          default: 900,
          minimum: 10,
          maximum: 86400,
        },
        gamma_limit: {
          type: 'integer',
          title: 'Gamma Limit',
          description: 'Number of events to request from Gamma per poll.',
          default: 100,
          minimum: 1,
          maximum: 500,
        },
        filter: { $ref: '#/$defs/MarketFilterConfig' },
      },
    },
  },
  {
    type: 'embedding',
    name: 'EmbeddingFilterV0',
    version: '0.1.0',
    module: 'openpoly.sections.embedding.minilm_v0',
    requires: ['market_data'],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'EmbeddingFilterConfig',
      properties: {
        embedding_model: {
          type: 'string',
          title: 'Embedding Model',
          description: 'Local sentence-transformer used to embed news + questions.',
          default: 'all-MiniLM-L6-v2',
        },
        top_k: {
          type: 'integer',
          title: 'Top K',
          description: 'Maximum candidate markets handed to the analyzer.',
          default: 10,
          minimum: 1,
          maximum: 100,
        },
        similarity_threshold: {
          type: 'number',
          title: 'Similarity Threshold',
          description: 'Minimum cosine similarity for a market to survive.',
          default: 0.35,
          minimum: 0,
          maximum: 1,
        },
        max_question_chars: {
          type: 'integer',
          title: 'Max Question Chars',
          description: 'Market question text is truncated to this before embedding.',
          default: 200,
          minimum: 20,
          maximum: 2000,
        },
        warm_interval_seconds: {
          type: 'integer',
          title: 'Warm Interval Seconds',
          description: 'Seconds between background catalog embedding refreshes.',
          default: 300,
          minimum: 30,
          maximum: 86400,
        },
      },
    },
  },
  {
    type: 'analyzer',
    name: 'LLMAnalyzerV0',
    version: '0.1.0',
    module: 'openpoly.sections.analyzer.llm_v0',
    requires: ['llm', 'market_data'],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'LLMAnalyzerConfig',
      properties: {
        llm_model: {
          type: 'string',
          title: 'Llm Model',
          default: 'claude-haiku-4-5',
          description:
            'Model id sent to the API. On the official Anthropic endpoint use a Claude id; on a third-party gateway use whatever id that gateway publishes.',
        },
        temperature: {
          type: 'number',
          title: 'Temperature',
          default: 0.2,
          minimum: 0,
          maximum: 1,
          description: 'Sampling temperature; ignored for claude-opus-4-7.',
        },
        api_key_ref: {
          type: 'string',
          title: 'Api Key Ref',
          default: 'env:ANTHROPIC_API_KEY',
          description: 'Reference to the LLM API key (env: / local: scheme).',
        },
        base_url: {
          type: 'string',
          title: 'Base Url',
          default: '',
          description: 'Third-party API base URL; empty = official Anthropic endpoint.',
        },
        extra_guidance: {
          type: 'string',
          title: 'Extra Guidance',
          default: '',
          description:
            "Optional extra guidance appended to the analyzer's system prompt. Cannot alter the structured-output contract.",
        },
        min_confidence: {
          type: 'string',
          title: 'Min Confidence',
          default: 'medium',
          enum: ['low', 'medium', 'high'],
        },
      },
    },
  },
  {
    type: 'entry',
    name: 'EdgeThresholdEntryV0',
    version: '0.4.0',
    module: 'openpoly.sections.entry.edge_threshold_v0',
    requires: ['order_book', 'market_data'],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'EdgeThresholdConfig',
      properties: {
        min_edge: {
          type: 'number',
          title: 'Min Edge',
          default: 0.05,
          minimum: 0,
          maximum: 1,
        },
        order_size_usd: {
          type: 'number',
          title: 'Order Size Usd',
          default: 10,
          minimum: 1,
          maximum: 100,
        },
        max_spread: {
          type: 'number',
          title: 'Max Spread',
          default: 0.05,
          minimum: 0,
          maximum: 0.5,
        },
        slippage_tolerance: {
          type: 'number',
          title: 'Slippage Tolerance',
          description: 'Reserved — dormant under the level-1 fill model (v1).',
          default: 0.02,
          minimum: 0,
          maximum: 0.2,
        },
        side_lock: {
          type: 'boolean',
          title: 'Side Lock',
          description: 'Lock to YES only; never buy NO.',
          default: false,
        },
        veto_enabled: {
          type: 'boolean',
          title: 'Veto Enabled',
          description:
            'Enable the late-buy veto. Off by default — run warn-only first and observe recent_move before enforcing.',
          default: false,
        },
        veto_window_min: {
          type: 'integer',
          title: 'Veto Window Min',
          description: 'Late-buy veto: price-move lookback window, in minutes.',
          default: 60,
          minimum: 1,
          maximum: 1440,
        },
        veto_move_threshold: {
          type: 'number',
          title: 'Veto Move Threshold',
          description:
            "Late-buy veto: skip the entry if the held side's token has already moved up by at least this much over the window.",
          default: 0.1,
          minimum: 0,
          maximum: 1,
        },
        same_market_cooldown_minutes: {
          type: 'integer',
          title: 'Same Market Cooldown Minutes',
          description:
            'Skip the entry if a position on the same (market, side) was opened or closed within this many minutes. 0 disables the check. Targets the repeated-loss-on-same-market pattern. Superseded by ``same_market_lifetime_lockout`` when that is True.',
          default: 0,
          minimum: 0,
          maximum: 1440,
        },
        same_market_lifetime_lockout: {
          type: 'boolean',
          title: 'Same Market Lifetime Lockout',
          description:
            'Strict mode: skip if ANY prior position exists on (market, side), regardless of when. One-shot-per-(market, side) across the lifetime of the strategy. When True, ``same_market_cooldown_minutes`` is ignored.',
          default: false,
        },
        size_edge_multiplier_max: {
          type: 'number',
          title: 'Size Edge Multiplier Max',
          description:
            "Scale the order with the edge: notional = order_size_usd × clamp(edge / min_edge, 1.0, this). 1.0 (the default) disables scaling entirely — sizing stays exactly order_size_usd / held_price, as it always was. Raise it ONLY after GET /api/analytics/calibration shows p_model is actually calibrated: each bucket's win rate close to its own midpoint, with n ≥ 100 behind the buckets being relied on. Betting more on a larger 'edge' computed from an uncalibrated probability only loses faster. heat_cap_usd, when set, still bounds the scaled notional.",
          default: 1,
          minimum: 1,
          maximum: 5,
        },
        heat_cap_usd: {
          type: 'number',
          title: 'Heat Cap Usd',
          description:
            "Skip the entry if the sum of (qty × avg_entry_price) across all currently-open positions is at or above this dollar amount. 0 disables the check. Caps total exposure during regimes where the analyzer's signal goes one-sided (cross-market correlated losses).",
          default: 0,
          minimum: 0,
          maximum: 10000,
        },
        kill_max_consecutive_losses: {
          type: 'integer',
          title: 'Kill Max Consecutive Losses',
          description:
            'Skip the entry if the most recent N closed positions are ALL losses (realized_pnl < 0). Catches regime change / strategy drift. 0 disables. Example: 5 stops new entries after 5 consecutive losers.',
          default: 0,
          minimum: 0,
          maximum: 100,
        },
        kill_daily_loss_usd: {
          type: 'number',
          title: 'Kill Daily Loss Usd',
          description:
            'Skip the entry if the sum of realized_pnl across positions closed in the last 24h is ≤ -kill_daily_loss_usd. 0 disables. Bounds single-day damage.',
          default: 0,
          minimum: 0,
          maximum: 10000,
        },
        kill_max_drawdown_usd: {
          type: 'number',
          title: 'Kill Max Drawdown Usd',
          description:
            'Skip the entry if the cumulative realized_pnl curve (all closed positions, chronological) has dropped this many dollars from its peak. 0 disables. Catches slow bleed across many small losses. Tighter than kill_daily_loss because it tracks ALL history, not just one day.',
          default: 0,
          minimum: 0,
          maximum: 10000,
        },
      },
    },
  },
  {
    type: 'exit',
    name: 'ThresholdExitV0',
    version: '0.3.0',
    module: 'openpoly.sections.exit.threshold_v0',
    requires: ['market_data', 'portfolio'],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'ThresholdExitConfig',
      properties: {
        take_profit_pct: {
          type: 'number',
          title: 'Take Profit Pct',
          description:
            'Take-profit ceiling, as a fraction of entry (0.20 = +20%). It caps every winner at this return, so it is OFF by default (see take_profit_enabled) and only applies once you switch it on: at +20% the trailing lock has not armed yet, so leaving it on means no position ever reaches the lock.',
          default: 0.2,
          minimum: 0,
          maximum: 10,
        },
        take_profit_enabled: {
          type: 'boolean',
          title: 'Take Profit Enabled',
          description:
            'Whether the take-profit ceiling is active. Off by default: the trailing peak-drawdown lock is the primary exit for winners, with the stop-loss underneath. Turn it on to cap every winner at take_profit_pct instead.',
          default: false,
        },
        stop_loss_pct: {
          type: 'number',
          title: 'Stop Loss Pct',
          description:
            'Close the position when its loss reaches this fraction (0.15 = -15%).',
          default: 0.15,
          minimum: 0,
          maximum: 1,
        },
        peak_drawdown_pct: {
          type: 'number',
          title: 'Peak Drawdown Pct',
          description:
            'Trailing lock: close when the price has retraced this fraction of the banked gain (peak - entry) from the peak. Only ever widens the trailing distance — the min_trail_ticks and spread floors below set its minimum.',
          default: 0.12,
          minimum: 0,
          maximum: 1,
        },
        min_trail_ticks: {
          type: 'integer',
          title: 'Min Trail Ticks',
          description:
            'Floor on the trailing distance, in price ticks. A percentage-only trail is tightest right after the position arms, where it can fall below a single tick and close on ordinary quote noise; two ticks is the smallest distance a real move can be distinguished from that noise.',
          default: 2,
          minimum: 0,
          maximum: 100,
        },
        tick_size: {
          type: 'number',
          title: 'Tick Size',
          description:
            'Price tick used for the min_trail_ticks floor (Polymarket CLOB is 0.01). A tick size carried on the marked position overrides this.',
          default: 0.01,
          maximum: 1,
          exclusiveMinimum: 0,
        },
        peak_meaningful_floor_usd: {
          type: 'number',
          title: 'Peak Meaningful Floor Usd',
          description:
            'Skip peak_drawdown unless the peak gain in USD exceeds this floor.',
          default: 1,
          minimum: 0,
        },
        peak_meaningful_floor_pct: {
          type: 'number',
          title: 'Peak Meaningful Floor Pct',
          description:
            'Skip peak_drawdown unless the peak gain exceeds this fraction of cost basis. Defaults to 30%: at grain-scale stakes the USD floor alone arms the trailing lock after a ~+10% move, where a retrace is noise rather than given-back profit. Arming at +30% means the lock only ever protects a real gain.',
          default: 0.3,
          minimum: 0,
          maximum: 1,
        },
      },
    },
  },
  {
    type: 'database',
    name: 'SqliteDatabase',
    version: '0.1.0',
    module: 'openpoly.sections.database.sqlite',
    requires: [],
    source: 'builtin',
    param_schema: {
      type: 'object',
      title: 'DatabaseConfig',
      properties: {
        order_book_retention_days: {
          type: 'number',
          title: 'Order Book Retention Days',
          description:
            'Delete order_book_snapshot rows older than this many days. 0 disables the prune entirely (rows are kept forever). The window has to outlive the longest-held position, because peak bootstrap rebuilds a trailing stop from snapshots taken since the position opened.',
          default: 7.0,
          minimum: 0,
        },
      },
    },
  },
]
