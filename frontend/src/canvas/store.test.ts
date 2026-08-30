import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MemoryStorage } from '../testing/memoryStorage'
import type { Template } from './templateIO'
import { STORAGE_KEY, TEMPLATE_VERSION } from './templateIO'

// `store.ts` reads localStorage at module scope, so every test imports it
// *after* seeding the stub. `vi.resetModules()` also resets the module-level
// node-id counter and the one-shot bootstrap guard.
type CanvasStore = typeof import('./store').useCanvasStore

async function freshStore(): Promise<CanvasStore> {
  vi.resetModules()
  const mod = await import('./store')
  return mod.useCanvasStore
}

function tpl(overrides: Partial<Template> = {}): Template {
  return {
    version: TEMPLATE_VERSION,
    name: 'from backend',
    nodes: [
      {
        id: 'n7',
        sectionType: 'embedding',
        position: { x: 5, y: 6 },
        config: { top_k: 3 },
      },
      {
        id: 'n8',
        sectionType: 'database',
        position: { x: 7, y: 8 },
        config: {},
      },
    ],
    edges: [{ source: 'n7', target: 'n8' }],
    ...overrides,
  }
}

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
  // Fake timers for the WHOLE suite, not just the debounce tests. Almost every
  // mutation here calls `scheduleAutosave`, which arms a 500ms `setTimeout`
  // that no test awaits. On real timers those survive the test that armed
  // them, fire inside a *later* test against a stale store module and that
  // test's `fetch` mock, and turn an exact call-count assertion into a
  // load-dependent failure. Faking them makes the pending timer ours to
  // discard in `afterEach`.
  vi.useFakeTimers()
  vi.stubGlobal('localStorage', new MemoryStorage())
  // Default: every network call fails, so a test that does not care about the
  // backend never leaves a floating unhandled fetch.
  fetchMock().mockRejectedValue(new Error('no backend in tests'))
})

afterEach(() => {
  // Drop every timer this test armed *before* handing the globals back, so
  // nothing can fire once `fetch` / `localStorage` are no longer stubbed.
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ---- startup --------------------------------------------------------------

describe('startup state', () => {
  it('falls back to the seed template when localStorage is empty', async () => {
    const useStore = await freshStore()
    const s = useStore.getState()
    expect(s.templateName).toBe('demo baseline')
    expect(s.nodes).toHaveLength(7)
    expect(s.edges).toHaveLength(6)
    expect(s.startupFlash).toBeNull()
    expect(s.saveStatus).toBe('saved')
    expect(s.serverRev).toBeNull()
    expect(s.isOnline).toBe(true)
  })

  it('adopts a saved v3 draft verbatim', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ name: 'my draft' })))
    const s = (await freshStore()).getState()
    expect(s.templateName).toBe('my draft')
    expect(s.nodes.map((n) => n.id)).toEqual(['n7', 'n8'])
    expect(s.nodes[0].data).toEqual({ sectionType: 'embedding', config: { top_k: 3 } })
    expect(s.edges).toEqual([{ id: 'e-n7-n8-0', source: 'n7', target: 'n8' }])
    expect(s.startupFlash).toBeNull()
  })

  it('flashes when a stored draft had to be migrated', async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 2,
        name: 'old',
        nodes: [
          { id: 'n1', sectionType: 'embedding', position: { x: 0, y: 0 }, config: {} },
          { id: 'n2', sectionType: 'database', position: { x: 0, y: 0 }, config: {} },
        ],
        edges: [],
      }),
    )
    const useStore = await freshStore()
    expect(useStore.getState().startupFlash).toBe(
      'Migrated draft to v3: linked Embedding → Database',
    )
    // consumeStartupFlash hands it over exactly once.
    expect(useStore.getState().consumeStartupFlash()).toBe(
      'Migrated draft to v3: linked Embedding → Database',
    )
    expect(useStore.getState().consumeStartupFlash()).toBeNull()
  })

  // --- malformed input ---
  it('falls back to the seed when the stored draft is unparseable', async () => {
    localStorage.setItem(STORAGE_KEY, 'not json at all')
    const s = (await freshStore()).getState()
    expect(s.templateName).toBe('demo baseline')
    expect(s.startupFlash).toBeNull()
  })

  it('resets to seed and flashes when the draft names a removed section', async () => {
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
    const s = (await freshStore()).getState()
    expect(s.templateName).toBe('demo baseline')
    expect(s.startupFlash).toBe('Saved draft used a removed section type — reset to seed')
  })
})

// ---- node / edge transitions ---------------------------------------------

describe('node and edge mutations', () => {
  it('adds one node per section type and selects it', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()

    useStore.getState().addSectionNode('analyzer', { x: 100, y: 200 })
    const added = useStore.getState().nodes.at(-1)
    expect(added?.data.sectionType).toBe('analyzer')
    expect(added?.position).toEqual({ x: 100, y: 200 })
    expect(useStore.getState().selectedNodeId).toBe(added?.id)
    // Defaults come from the offline catalog fallback.
    expect(added?.data.config.min_confidence).toBe('medium')

    // Second attempt at the same type is a no-op — one section per type.
    useStore.getState().addSectionNode('analyzer', { x: 0, y: 0 })
    expect(
      useStore.getState().nodes.filter((n) => n.data.sectionType === 'analyzer'),
    ).toHaveLength(1)
  })

  it('never reuses a node id already present in a loaded template', async () => {
    const useStore = await freshStore()
    useStore.getState().loadTemplate(
      tpl({
        nodes: [
          {
            id: 'n42',
            sectionType: 'embedding',
            position: { x: 0, y: 0 },
            config: {},
          },
        ],
        edges: [],
      }),
    )
    useStore.getState().addSectionNode('analyzer', { x: 0, y: 0 })
    expect(useStore.getState().nodes.at(-1)?.id).toBe('n43')
  })

  it('updates one config key without touching the rest', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().updateNodeConfig('n7', 'top_k', 25)
    expect(useStore.getState().nodes[0].data.config).toEqual({ top_k: 25 })
    // Other nodes are untouched.
    expect(useStore.getState().nodes[1].data.config).toEqual({})
  })

  it('replaces a node config wholesale on bulk update', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().updateNodeConfigBulk('n7', { similarity_threshold: 0.5 })
    expect(useStore.getState().nodes[0].data.config).toEqual({
      similarity_threshold: 0.5,
    })
  })

  it('accepts a connection the edge rules allow', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ edges: [] })))
    const useStore = await freshStore()
    useStore.getState().onConnect({
      source: 'n7',
      target: 'n8',
      sourceHandle: null,
      targetHandle: null,
    })
    expect(useStore.getState().edges).toHaveLength(1)
  })

  it('rejects a connection the edge rules forbid', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ edges: [] })))
    const useStore = await freshStore()
    // database → embedding is not a legal pair, and n7 → n7 is a self-loop.
    useStore.getState().onConnect({
      source: 'n8',
      target: 'n7',
      sourceHandle: null,
      targetHandle: null,
    })
    useStore.getState().onConnect({
      source: 'n7',
      target: 'n7',
      sourceHandle: null,
      targetHandle: null,
    })
    expect(useStore.getState().edges).toEqual([])
  })

  it('rejects a duplicate of an edge that already exists', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().onConnect({
      source: 'n7',
      target: 'n8',
      sourceHandle: null,
      targetHandle: null,
    })
    expect(useStore.getState().edges).toHaveLength(1)
  })

  it('drops a node through onNodesChange', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().onNodesChange([{ type: 'remove', id: 'n8' }])
    expect(useStore.getState().nodes.map((n) => n.id)).toEqual(['n7'])
  })

  it('drops an edge through onEdgesChange', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().onEdgesChange([{ type: 'remove', id: 'e-n7-n8-0' }])
    expect(useStore.getState().edges).toEqual([])
  })
})

// ---- serialize / load -----------------------------------------------------

describe('serialize and loadTemplate', () => {
  it('serializes back to the exact template it was loaded from', async () => {
    const useStore = await freshStore()
    const source = tpl()
    useStore.getState().loadTemplate(source)
    expect(useStore.getState().serialize()).toEqual(source)
  })

  it('carries a renamed template into the serialized form', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().setTemplateName('renamed')
    expect(useStore.getState().serialize().name).toBe('renamed')
    expect(useStore.getState().serialize().version).toBe(TEMPLATE_VERSION)
  })

  it('clears the selection and resets to seed', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    useStore.getState().setSelectedNodeId('n7')
    expect(useStore.getState().selectedNodeId).toBe('n7')
    useStore.getState().resetToSeed()
    expect(useStore.getState().selectedNodeId).toBeNull()
    expect(useStore.getState().templateName).toBe('demo baseline')
    expect(useStore.getState().nodes).toHaveLength(7)
  })
})

// ---- autosave -------------------------------------------------------------

describe('autosave', () => {
  it('debounces edits into one PUT and records the returned rev', async () => {
    vi.useFakeTimers()
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { rev: 'sha-new' }))

    useStore.getState().setTemplateName('a')
    useStore.getState().setTemplateName('ab')
    useStore.getState().setTemplateName('abc')
    expect(useStore.getState().saveStatus).toBe('saving')
    expect(fetchFn).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(600)
    expect(fetchFn).toHaveBeenCalledTimes(1)
    expect(useStore.getState().saveStatus).toBe('saved')
    expect(useStore.getState().serverRev).toBe('sha-new')
    // localStorage mirrors the pushed draft.
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}').name).toBe('abc')
  })

  it('goes offline when the autosave PUT cannot reach the backend', async () => {
    vi.useFakeTimers()
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    fetchMock().mockRejectedValue(new Error('ECONNREFUSED'))

    useStore.getState().setTemplateName('offline edit')
    await vi.advanceTimersByTimeAsync(600)
    expect(useStore.getState().saveStatus).toBe('offline')
    expect(useStore.getState().isOnline).toBe(false)
    // The local draft is still authoritative while offline.
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}').name).toBe(
      'offline edit',
    )
  })

  it('parks a 409 as a conflict and stops autosaving until it is resolved', async () => {
    vi.useFakeTimers()
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(
      res(409, {
        detail: { current_rev: 'sha-theirs', template: tpl({ name: 'theirs' }) },
      }),
    )

    useStore.getState().setTemplateName('mine')
    await vi.advanceTimersByTimeAsync(600)
    const conflict = useStore.getState().conflict
    expect(conflict?.mine.name).toBe('mine')
    expect(conflict?.theirs.name).toBe('theirs')
    expect(conflict?.theirsRev).toBe('sha-theirs')

    // Further edits must not fire another PUT while the conflict stands.
    useStore.getState().setTemplateName('mine again')
    await vi.advanceTimersByTimeAsync(600)
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })

  it('arms exactly one pending debounce timer for an edit burst', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()

    useStore.getState().setTemplateName('a')
    useStore.getState().setTemplateName('ab')
    // Under the suite's fake timers the debounce is inspectable — and, more to
    // the point, cancellable by `afterEach`.
    expect(vi.getTimerCount()).toBe(1)
  })
  // No companion "the next test starts with 0 timers" case: `beforeEach` calls
  // `useFakeTimers()`, which installs a fresh clock, so that assertion holds
  // whether or not `afterEach` clears anything. It cannot fail, so it guards
  // nothing.
})

// ---- bootstrapFromBackend -------------------------------------------------

describe('bootstrapFromBackend', () => {
  it('adopts the server canvas and mirrors it to localStorage', async () => {
    const useStore = await freshStore()
    fetchMock().mockResolvedValue(res(200, { ...tpl(), rev: 'sha-1' }))

    await useStore.getState().bootstrapFromBackend()
    const s = useStore.getState()
    expect(s.templateName).toBe('from backend')
    expect(s.nodes.map((n) => n.id)).toEqual(['n7', 'n8'])
    expect(s.serverRev).toBe('sha-1')
    expect(s.isOnline).toBe(true)
    expect(s.startupFlash).toBe('Loaded canvas from backend')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}').name).toBe(
      'from backend',
    )
  })

  it('runs at most once per module instance', async () => {
    const useStore = await freshStore()
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { ...tpl(), rev: 'sha-1' }))
    await useStore.getState().bootstrapFromBackend()
    await useStore.getState().bootstrapFromBackend()
    expect(fetchFn).toHaveBeenCalledTimes(1)
  })

  it('keeps the local draft when the backend has no canvas yet', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ name: 'local only' })))
    const useStore = await freshStore()
    fetchMock().mockResolvedValue(res(404, {}))

    await useStore.getState().bootstrapFromBackend()
    expect(useStore.getState().templateName).toBe('local only')
    expect(useStore.getState().serverRev).toBeNull()
    expect(useStore.getState().isOnline).toBe(true)
  })

  it('goes offline and keeps the local draft when the backend is unreachable', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ name: 'local only' })))
    const useStore = await freshStore()
    fetchMock().mockRejectedValue(new Error('down'))

    await useStore.getState().bootstrapFromBackend()
    const s = useStore.getState()
    expect(s.templateName).toBe('local only')
    expect(s.isOnline).toBe(false)
    expect(s.saveStatus).toBe('offline')
    expect(s.startupFlash).toBe('Backend unreachable — working from local draft')
  })

  // --- malformed input ---
  it('treats a structurally broken backend body as unreachable', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ name: 'local only' })))
    const useStore = await freshStore()
    fetchMock().mockResolvedValue(res(200, { version: 3, nodes: 'nope', edges: 0 }))

    await useStore.getState().bootstrapFromBackend()
    const s = useStore.getState()
    expect(s.templateName).toBe('local only')
    expect(s.isOnline).toBe(false)
    expect(s.saveStatus).toBe('offline')
  })

  it('keeps the local draft when the backend canvas names a removed section', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl({ name: 'local only' })))
    const useStore = await freshStore()
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

    await useStore.getState().bootstrapFromBackend()
    const s = useStore.getState()
    expect(s.templateName).toBe('local only')
    expect(s.isOnline).toBe(true)
    expect(s.startupFlash).toBe(
      'Backend canvas uses a removed section type — using local draft',
    )
  })
})

// ---- resolveConflict ------------------------------------------------------

describe('resolveConflict', () => {
  async function storeInConflict(): Promise<CanvasStore> {
    vi.useFakeTimers()
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tpl()))
    const useStore = await freshStore()
    fetchMock().mockResolvedValue(
      res(409, {
        detail: { current_rev: 'sha-theirs', template: tpl({ name: 'theirs' }) },
      }),
    )
    useStore.getState().setTemplateName('mine')
    await vi.advanceTimersByTimeAsync(600)
    expect(useStore.getState().conflict).not.toBeNull()
    return useStore
  }

  it('is a no-op when there is no conflict pending', async () => {
    const useStore = await freshStore()
    await useStore.getState().resolveConflict('dismiss')
    expect(useStore.getState().conflict).toBeNull()
  })

  it('dismiss clears the modal without changing the canvas', async () => {
    const useStore = await storeInConflict()
    await useStore.getState().resolveConflict('dismiss')
    expect(useStore.getState().conflict).toBeNull()
    expect(useStore.getState().templateName).toBe('mine')
  })

  it('take_theirs adopts the server template and its rev', async () => {
    const useStore = await storeInConflict()
    await useStore.getState().resolveConflict('take_theirs')
    const s = useStore.getState()
    expect(s.templateName).toBe('theirs')
    expect(s.serverRev).toBe('sha-theirs')
    expect(s.conflict).toBeNull()
    expect(s.saveStatus).toBe('saved')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}').name).toBe('theirs')
  })

  it('keep_mine force-overwrites the backend with If-Match: *', async () => {
    const useStore = await storeInConflict()
    const fetchFn = fetchMock()
    fetchFn.mockResolvedValue(res(200, { rev: 'sha-forced' }))

    await useStore.getState().resolveConflict('keep_mine')
    const headers = (fetchFn.mock.calls[0][1] as RequestInit).headers as Record<
      string,
      string
    >
    expect(headers['If-Match']).toBe('*')
    const s = useStore.getState()
    expect(s.serverRev).toBe('sha-forced')
    expect(s.conflict).toBeNull()
    expect(s.templateName).toBe('mine')
  })

  it('keeps the conflict pending when the force overwrite fails', async () => {
    const useStore = await storeInConflict()
    fetchMock().mockRejectedValue(new Error('still down'))
    vi.spyOn(console, 'error').mockImplementation(() => {})

    await useStore.getState().resolveConflict('keep_mine')
    expect(useStore.getState().conflict).not.toBeNull()
    expect(useStore.getState().saveStatus).toBe('saved')
  })
})
