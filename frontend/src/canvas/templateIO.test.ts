import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { API_TOKEN_HEADER, API_TOKEN_STORAGE_KEY } from '../lib/apiClient'
import { MemoryStorage } from '../testing/memoryStorage'
import {
  fetchTemplateFromBackend,
  loadFromStorage,
  pushTemplateToBackend,
  saveToStorage,
  STORAGE_KEY,
  TEMPLATE_VERSION,
  type Template,
} from './templateIO'

// ---- helpers --------------------------------------------------------------

function v3(overrides: Partial<Template> = {}): Template {
  return {
    version: TEMPLATE_VERSION,
    name: 'unit',
    nodes: [
      {
        id: 'n1',
        sectionType: 'embedding',
        position: { x: 1, y: 2 },
        config: { top_k: 10 },
      },
      {
        id: 'n2',
        sectionType: 'database',
        position: { x: 3, y: 4 },
        config: {},
      },
    ],
    edges: [{ source: 'n1', target: 'n2' }],
    ...overrides,
  }
}

/** Minimal `Response` stand-in — the module only touches ok/status/json. */
function res(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

function fetchMock() {
  const fn = vi.fn<(...args: unknown[]) => Promise<Response>>()
  vi.stubGlobal('fetch', fn)
  return fn
}

beforeEach(() => {
  vi.stubGlobal('localStorage', new MemoryStorage())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ---- localStorage round-trip ---------------------------------------------

describe('loadFromStorage / saveToStorage', () => {
  it('returns null when nothing has been saved', () => {
    expect(loadFromStorage()).toBeNull()
  })

  it('round-trips a current-version template without migrating', () => {
    const tpl = v3()
    saveToStorage(tpl)
    const loaded = loadFromStorage()
    expect(loaded).toEqual({ status: 'ok', template: tpl, migrated: false })
  })

  it('rejects a draft referencing a section type this build removed', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: TEMPLATE_VERSION,
        name: 'legacy',
        nodes: [
          { id: 'n1', sectionType: 'trader', position: { x: 0, y: 0 }, config: {} },
        ],
        edges: [],
      }),
    )
    expect(loadFromStorage()).toEqual({ status: 'incompatible' })
  })

  it('returns null for a version this build cannot reach by migration', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: 99, name: 'future', nodes: [], edges: [] }),
    )
    expect(loadFromStorage()).toBeNull()
  })

  // --- malformed input ---
  it('returns null on malformed JSON instead of throwing', () => {
    localStorage.setItem(STORAGE_KEY, '{ this is not json')
    expect(loadFromStorage()).toBeNull()
  })

  it('returns null when nodes/edges are not arrays', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: TEMPLATE_VERSION, name: 'x', nodes: {}, edges: null }),
    )
    expect(loadFromStorage()).toBeNull()
  })
})

// ---- migration chain ------------------------------------------------------

describe('migration chain', () => {
  it('climbs a v1 draft to v3, grafting a news_source above the analyzer', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 1,
        name: 'v1 draft',
        nodes: [
          {
            id: 'a1',
            sectionType: 'analyzer',
            position: { x: 10, y: 400 },
            config: {},
          },
        ],
        edges: [],
      }),
    )
    const loaded = loadFromStorage()
    expect(loaded?.status).toBe('ok')
    if (loaded?.status !== 'ok') return
    expect(loaded.migrated).toBe(true)
    expect(loaded.template.version).toBe(TEMPLATE_VERSION)
    const news = loaded.template.nodes.find((n) => n.sectionType === 'news_source')
    expect(news).toBeDefined()
    // Grafted above the analyzer, and wired into it.
    expect(news?.position).toEqual({ x: 10, y: 200 })
    expect(loaded.template.edges).toContainEqual({
      source: news?.id,
      target: 'a1',
    })
    // The v1 draft had no news_source config; the migration seeds defaults.
    expect(news?.config.endpoint).toBe('wss://api.tradingnews.press/v1/stream')
  })

  it('migrates a v1 draft without an analyzer by version bump alone', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: 1, name: 'headless', nodes: [], edges: [] }),
    )
    const loaded = loadFromStorage()
    expect(loaded).toEqual({
      status: 'ok',
      migrated: true,
      template: { version: 3, name: 'headless', nodes: [], edges: [] },
    })
  })

  it('adds the embedding → database edge when climbing v2 → v3', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 2,
        name: 'v2 draft',
        nodes: [
          { id: 'e1', sectionType: 'embedding', position: { x: 0, y: 0 }, config: {} },
          { id: 'd1', sectionType: 'database', position: { x: 0, y: 0 }, config: {} },
        ],
        edges: [],
      }),
    )
    const loaded = loadFromStorage()
    expect(loaded?.status).toBe('ok')
    if (loaded?.status !== 'ok') return
    expect(loaded.migrated).toBe(true)
    expect(loaded.template.edges).toEqual([{ source: 'e1', target: 'd1' }])
  })

  it('does not duplicate an embedding → database edge the operator drew', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 2,
        name: 'v2 draft',
        nodes: [
          { id: 'e1', sectionType: 'embedding', position: { x: 0, y: 0 }, config: {} },
          { id: 'd1', sectionType: 'database', position: { x: 0, y: 0 }, config: {} },
        ],
        edges: [{ source: 'e1', target: 'd1' }],
      }),
    )
    const loaded = loadFromStorage()
    if (loaded?.status !== 'ok') throw new Error('expected ok')
    expect(loaded.template.edges).toEqual([{ source: 'e1', target: 'd1' }])
  })
})

// ---- GET /api/canvas/template --------------------------------------------

describe('fetchTemplateFromBackend', () => {
  it('adopts a well-formed backend canvas with its rev', async () => {
    fetchMock().mockResolvedValue(res(200, { ...v3(), rev: 'sha-1' }))
    const result = await fetchTemplateFromBackend()
    if (result.status !== 'ok') throw new Error(`expected ok, got ${result.status}`)
    expect(result.rev).toBe('sha-1')
    expect(result.migrated).toBe(false)
    expect(result.template.name).toBe('unit')
    expect(result.template.version).toBe(TEMPLATE_VERSION)
    expect(result.template.nodes).toEqual(v3().nodes)
    expect(result.template.edges).toEqual(v3().edges)
  })

  it('reports 404 as an empty backend rather than an error', async () => {
    fetchMock().mockResolvedValue(res(404, {}))
    expect(await fetchTemplateFromBackend()).toEqual({ status: 'empty' })
  })

  it('surfaces a non-2xx status as a network error', async () => {
    fetchMock().mockResolvedValue(res(500, {}))
    expect(await fetchTemplateFromBackend()).toEqual({
      status: 'network_error',
      error: 'HTTP 500',
    })
  })

  it('treats a missing rev as a soft network problem', async () => {
    fetchMock().mockResolvedValue(res(200, v3()))
    expect(await fetchTemplateFromBackend()).toEqual({
      status: 'network_error',
      error: 'missing rev in response',
    })
  })

  it('reports a removed section type as incompatible', async () => {
    fetchMock().mockResolvedValue(
      res(200, {
        version: TEMPLATE_VERSION,
        name: 'legacy',
        nodes: [
          { id: 'n1', sectionType: 'trader', position: { x: 0, y: 0 }, config: {} },
        ],
        edges: [],
        rev: 'sha-1',
      }),
    )
    expect(await fetchTemplateFromBackend()).toEqual({ status: 'incompatible' })
  })

  it('propagates a thrown fetch as a network error', async () => {
    fetchMock().mockRejectedValue(new Error('offline'))
    expect(await fetchTemplateFromBackend()).toEqual({
      status: 'network_error',
      error: 'offline',
    })
  })

  // --- malformed input ---
  it('rejects a non-object body', async () => {
    fetchMock().mockResolvedValue(res(200, null))
    expect(await fetchTemplateFromBackend()).toEqual({
      status: 'network_error',
      error: 'bad response shape',
    })
  })

  it('rejects a body whose nodes/edges are not arrays', async () => {
    fetchMock().mockResolvedValue(
      res(200, { version: 3, name: 'x', nodes: 'nope', edges: 1, rev: 'r' }),
    )
    expect(await fetchTemplateFromBackend()).toEqual({
      status: 'network_error',
      error: 'missing nodes/edges',
    })
  })
})

// ---- PUT /api/canvas/template --------------------------------------------

describe('pushTemplateToBackend', () => {
  it('sends If-Match with the known rev and returns the new one', async () => {
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { rev: 'sha-2' }))
    const result = await pushTemplateToBackend(v3(), 'sha-1')
    expect(result).toEqual({ status: 'ok', rev: 'sha-2' })
    const init = fetchFn.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('PUT')
    expect(init.headers).toMatchObject({
      'Content-Type': 'application/json',
      'If-Match': 'sha-1',
    })
    expect(JSON.parse(init.body as string)).toEqual(v3())
  })

  it('omits If-Match on the first write (no rev observed yet)', async () => {
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { rev: 'sha-1' }))
    await pushTemplateToBackend(v3(), null)
    const headers = (fetchFn.mock.calls[0][1] as RequestInit).headers as Record<
      string,
      string
    >
    expect(headers['If-Match']).toBeUndefined()
  })

  it('sends the wildcard If-Match for a force overwrite', async () => {
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { rev: 'sha-9' }))
    await pushTemplateToBackend(v3(), '*')
    const headers = (fetchFn.mock.calls[0][1] as RequestInit).headers as Record<
      string,
      string
    >
    expect(headers['If-Match']).toBe('*')
  })

  it('returns an empty rev when the 200 body omits it', async () => {
    fetchMock().mockResolvedValue(res(200, {}))
    expect(await pushTemplateToBackend(v3(), 'sha-1')).toEqual({
      status: 'ok',
      rev: '',
    })
  })

  it('parses a 409 into a conflict carrying the current template', async () => {
    fetchMock().mockResolvedValue(
      res(409, {
        detail: { current_rev: 'sha-theirs', template: v3({ name: 'theirs' }) },
      }),
    )
    expect(await pushTemplateToBackend(v3(), 'sha-mine')).toEqual({
      status: 'conflict',
      current_rev: 'sha-theirs',
      current_template: v3({ name: 'theirs' }),
    })
  })

  it('maps a 400 to bad_request with the server message', async () => {
    fetchMock().mockResolvedValue(
      res(400, { detail: { error: 'bad_shape', message: 'node id repeated' } }),
    )
    expect(await pushTemplateToBackend(v3(), 'sha-1')).toEqual({
      status: 'bad_request',
      error: 'node id repeated',
    })
  })

  it('reports a thrown fetch as a network error', async () => {
    fetchMock().mockRejectedValue(new Error('connection refused'))
    expect(await pushTemplateToBackend(v3(), 'sha-1')).toEqual({
      status: 'network_error',
      error: 'connection refused',
    })
  })

  // --- malformed input ---
  it('refuses a 409 whose body does not carry a usable template', async () => {
    fetchMock().mockResolvedValue(res(409, { detail: { current_rev: 'sha-theirs' } }))
    expect(await pushTemplateToBackend(v3(), 'sha-mine')).toEqual({
      status: 'network_error',
      error: '409 with malformed current_template',
    })
  })

  it('attaches the stored API token to the PUT', async () => {
    localStorage.setItem(API_TOKEN_STORAGE_KEY, 'shared-secret')
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { rev: 'sha-2' }))
    await pushTemplateToBackend(v3(), 'sha-1')
    const headers = new Headers(
      (fetchFn.mock.calls[0][1] as RequestInit).headers as HeadersInit,
    )
    expect(headers.get(API_TOKEN_HEADER)).toBe('shared-secret')
    // The optimistic-lock header survives the token being layered on.
    expect(headers.get('If-Match')).toBe('sha-1')
  })

  it('leaves the GET unauthenticated — reads are open by design', async () => {
    localStorage.setItem(API_TOKEN_STORAGE_KEY, 'shared-secret')
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { ...v3(), rev: 'sha-1' }))
    await fetchTemplateFromBackend()
    expect(fetchFn.mock.calls[0][1]).toBeUndefined()
  })

  it('refuses a 409 whose template uses a removed section type', async () => {
    fetchMock().mockResolvedValue(
      res(409, {
        detail: {
          current_rev: 'sha-theirs',
          template: {
            version: TEMPLATE_VERSION,
            name: 'theirs',
            nodes: [
              { id: 'n1', sectionType: 'trader', position: { x: 0, y: 0 }, config: {} },
            ],
            edges: [],
          },
        },
      }),
    )
    expect(await pushTemplateToBackend(v3(), 'sha-mine')).toEqual({
      status: 'network_error',
      error: '409 with malformed current_template',
    })
  })
})
