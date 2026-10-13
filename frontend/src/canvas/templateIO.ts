import { apiFetch } from '../lib/apiClient'
import {
  defaultConfigForType,
  ENTRY_V1_COOLDOWN_MINUTES,
  MOCK_RUNTIME_CATALOG,
  SECTION_ORDER,
} from '../sections/catalog'
import type { ConfigValues, SectionType } from '../sections/types'

export const TEMPLATE_VERSION = 4
export const STORAGE_KEY = 'openpoly.template.draft'

export type TemplateNode = {
  id: string
  sectionType: SectionType
  position: { x: number; y: number }
  config: ConfigValues
}

export type TemplateEdge = {
  source: string
  target: string
}

export type Template = {
  version: typeof TEMPLATE_VERSION
  name: string
  nodes: TemplateNode[]
  edges: TemplateEdge[]
}

// `null` means no draft at all; `incompatible` means a draft exists but
// references section types this build no longer knows about.
export type LoadResult =
  | { status: 'ok'; template: Template; migrated: boolean }
  | { status: 'incompatible' }

type StoredTemplate = {
  version: number
  name: string
  nodes: TemplateNode[]
  edges: TemplateEdge[]
}

const KNOWN_SECTION_TYPES = new Set<string>(SECTION_ORDER)

// A draft is only usable if every node maps to a section type the current
// build still knows about. Legacy types (e.g. 'trader', removed when it was
// split into entry/exit) render without handles and silently break every
// edge attached to them — so reject the whole draft and fall back to seed.
// The version number alone can't catch this: section types can change without
// anyone remembering to bump TEMPLATE_VERSION.
function hasOnlyKnownSections(t: Template): boolean {
  return t.nodes.every((n) => KNOWN_SECTION_TYPES.has(n.sectionType))
}

export function loadFromStorage(): LoadResult | null {
  if (typeof localStorage === 'undefined') return null
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as StoredTemplate
    if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.edges)) return null
    // Run the migration chain step by step so a v1 draft climbs v1→…→v4.
    const { stored, migrated } = migrate(parsed)
    if (stored.version !== TEMPLATE_VERSION) return null
    const template = stored as Template
    if (!hasOnlyKnownSections(template)) return { status: 'incompatible' }
    return { status: 'ok', template, migrated }
  } catch {
    return null
  }
}

export function saveToStorage(template: Template): void {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(STORAGE_KEY, JSON.stringify(template))
}

// ---- Backend sync (canvas-sync v2) ---------------------------------------
//
// Backend is canonical: GET returns {...template, rev}; PUT requires
// If-Match: <rev>. Stale rev → 409 with the current template in the body so
// the frontend can render a conflict UI. Wildcard ``If-Match: *`` is the
// explicit force-overwrite escape hatch.
//
// FetchResult / PushResult are tagged unions so callers handle every state
// explicitly — no silent network failures (today's stale-localStorage bug
// came from a swallowed PUT).

const BACKEND_TEMPLATE_URL = '/api/canvas/template'

export type FetchOk = {
  status: 'ok'
  template: Template
  rev: string
  migrated: boolean
}
export type FetchResult =
  | FetchOk
  | { status: 'empty' }              // backend has nothing yet
  | { status: 'incompatible' }       // template uses removed section types
  | { status: 'network_error'; error: string }

export type PushOk = { status: 'ok'; rev: string }
export type PushConflict = {
  status: 'conflict'
  current_rev: string
  current_template: Template
}
export type PushAuthError = {
  status: 'auth_error'
  /** 401 (missing/wrong X-OpenPoly-Token) or 403 (cross-origin write guard). */
  httpStatus: number
  error: string
}

export type PushResult =
  | PushOk
  | PushConflict
  | { status: 'network_error'; error: string }
  | { status: 'bad_request'; error: string }
  | PushAuthError


async function _parseTemplate(raw: unknown): Promise<FetchResult> {
  if (typeof raw !== 'object' || raw === null) {
    return { status: 'network_error', error: 'bad response shape' }
  }
  const r = raw as StoredTemplate & { rev?: string }
  if (!Array.isArray(r.nodes) || !Array.isArray(r.edges)) {
    return { status: 'network_error', error: 'missing nodes/edges' }
  }
  // Migration chain — same as loadFromStorage, so an old backend canvas
  // self-upgrades on the way in.
  const { stored, migrated } = migrate(r)
  if (stored.version !== TEMPLATE_VERSION) {
    return { status: 'incompatible' }
  }
  const template = stored as Template
  if (!hasOnlyKnownSections(template)) {
    return { status: 'incompatible' }
  }
  const rev = typeof r.rev === 'string' ? r.rev : ''
  if (!rev) {
    // Backend should always include rev; treat absence as a soft network
    // problem so we don't enter the optimistic-lock loop with an empty rev.
    return { status: 'network_error', error: 'missing rev in response' }
  }
  return { status: 'ok', template, rev, migrated }
}

export async function fetchTemplateFromBackend(): Promise<FetchResult> {
  try {
    const r = await fetch(BACKEND_TEMPLATE_URL)
    if (r.status === 404) return { status: 'empty' }
    if (!r.ok) {
      return {
        status: 'network_error',
        error: `HTTP ${r.status}`,
      }
    }
    return _parseTemplate(await r.json())
  } catch (e) {
    return {
      status: 'network_error',
      error: e instanceof Error ? e.message : String(e),
    }
  }
}

/**
 * PUT the template back with optimistic-lock semantics.
 *
 * - `expectedRev = null`: first write (no template on disk yet). Backend
 *   accepts only when its own state is also empty; otherwise returns 409.
 * - `expectedRev = '*'`: force-overwrite (operator chose Keep Mine in
 *   ConflictDialog).
 * - `expectedRev = <sha>`: normal autosave; 409 if backend has moved.
 */
export async function pushTemplateToBackend(
  template: Template,
  expectedRev: string | '*' | null,
): Promise<PushResult> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (expectedRev !== null) headers['If-Match'] = expectedRev
  let r: Response
  try {
    r = await apiFetch(BACKEND_TEMPLATE_URL, {
      method: 'PUT',
      headers,
      body: JSON.stringify(template),
    })
  } catch (e) {
    return {
      status: 'network_error',
      error: e instanceof Error ? e.message : String(e),
    }
  }
  if (r.status === 409) {
    const body = (await r.json().catch(() => ({}))) as {
      detail?: { current_rev?: string; template?: StoredTemplate }
    }
    const detail = body.detail ?? {}
    const tplRaw = detail.template
    // The 409 body carries the current canvas; parse it the same way we
    // would a fresh GET — migration chain included, so a backend canvas still
    // at an older version is a conflict, not a network error — and the caller
    // can pop the diff dialog immediately without a second round trip.
    if (
      detail.current_rev &&
      tplRaw &&
      Array.isArray(tplRaw.nodes) &&
      Array.isArray(tplRaw.edges)
    ) {
      const { stored } = migrate(tplRaw)
      if (stored.version === TEMPLATE_VERSION && hasOnlyKnownSections(stored as Template)) {
        return {
          status: 'conflict',
          current_rev: detail.current_rev,
          current_template: stored as Template,
        }
      }
    }
    // Fallback if shape is off — caller should re-fetch.
    return {
      status: 'network_error',
      error: '409 with malformed current_template',
    }
  }
  if (r.status === 400) {
    const body = (await r.json().catch(() => ({}))) as {
      detail?: { error?: string; message?: string }
    }
    return {
      status: 'bad_request',
      error: body.detail?.message ?? `HTTP 400`,
    }
  }
  if (r.status === 401 || r.status === 403) {
    // The backend is up and answering — the write was REFUSED (missing/stale
    // API token, or the cross-origin write guard). Folding this into
    // network_error flipped the canvas to "Backend unreachable" and sent the
    // operator to debug connectivity instead of Keys → API token.
    const body = (await r.json().catch(() => ({}))) as {
      detail?: { message?: string }
      message?: string
    }
    return {
      status: 'auth_error',
      httpStatus: r.status,
      error: body.detail?.message ?? body.message ?? `HTTP ${r.status}`,
    }
  }
  if (!r.ok) {
    return { status: 'network_error', error: `HTTP ${r.status}` }
  }
  const body = (await r.json()) as { rev?: string }
  return { status: 'ok', rev: typeof body.rev === 'string' ? body.rev : '' }
}

// v1 → v2: v1 drafts predate the news_source section — graft one in above the
// analyzer so the pipeline has a head. Returns a v2-shaped draft; the caller's
// chain carries it on to v3.
function migrateV1toV2(v1: StoredTemplate): StoredTemplate {
  const analyzer = v1.nodes.find((n) => n.sectionType === 'analyzer')
  if (!analyzer) {
    return { version: 2, name: v1.name, nodes: v1.nodes, edges: v1.edges }
  }
  const newsId = 'news_source-migrated'
  const newsNode: TemplateNode = {
    id: newsId,
    sectionType: 'news_source',
    position: { x: analyzer.position.x, y: analyzer.position.y - 200 },
    config: defaultConfigForType('news_source', MOCK_RUNTIME_CATALOG),
  }
  return {
    version: 2,
    name: v1.name,
    nodes: [newsNode, ...v1.nodes],
    edges: [{ source: newsId, target: analyzer.id }, ...v1.edges],
  }
}

// v2 → v3: the embedding section persists into the database section (the
// market_embedding vector cache). Older drafts predate that write-to-DB edge —
// add it so the canvas shows the dependency. Skipped if either node is absent
// or the edge already exists (the user may have drawn it manually).
function migrateV2toV3(v2: StoredTemplate): StoredTemplate {
  const embedding = v2.nodes.find((n) => n.sectionType === 'embedding')
  const database = v2.nodes.find((n) => n.sectionType === 'database')
  let edges = v2.edges
  if (embedding && database) {
    const exists = edges.some(
      (e) => e.source === embedding.id && e.target === database.id,
    )
    if (!exists) {
      edges = [...edges, { source: embedding.id, target: database.id }]
    }
  }
  return { version: 3, name: v2.name, nodes: v2.nodes, edges }
}

// v3 → v4: entry v1 defaults ``same_market_cooldown_minutes`` to 60, but a
// canvas saved while entry v0 was active persisted v0's default of 0 — which,
// being explicit, overrode v1's default and silently disabled the cooldown.
// Lift an explicit 0 to 60 once. A v4 canvas is never migrated again, so a
// user who deliberately sets 0 after the upgrade keeps it. The backend applies
// the same step when it reads a v3 canvas (``canvas_store.upgrade_template``),
// so the runtime agrees before the frontend has saved the upgraded canvas.

function migrateV3toV4(v3: StoredTemplate): StoredTemplate {
  const nodes = v3.nodes.map((n) =>
    n.sectionType === 'entry' && n.config?.same_market_cooldown_minutes === 0
      ? {
          ...n,
          config: { ...n.config, same_market_cooldown_minutes: ENTRY_V1_COOLDOWN_MINUTES },
        }
      : n,
  )
  return { version: 4, name: v3.name, nodes, edges: v3.edges }
}

// The whole chain, one step per version, so any older template climbs to the
// current one.
function migrate(input: StoredTemplate): { stored: StoredTemplate; migrated: boolean } {
  let stored = input
  let migrated = false
  if (stored.version === 1) {
    stored = migrateV1toV2(stored)
    migrated = true
  }
  if (stored.version === 2) {
    stored = migrateV2toV3(stored)
    migrated = true
  }
  if (stored.version === 3) {
    stored = migrateV3toV4(stored)
    migrated = true
  }
  return { stored, migrated }
}
