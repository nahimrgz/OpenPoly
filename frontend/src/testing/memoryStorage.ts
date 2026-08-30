/**
 * In-memory `Storage` implementation for tests.
 *
 * `canvas/templateIO.ts` and `canvas/store.ts` both talk to `localStorage`,
 * and the store reads it at *module scope* — so a test that wants a specific
 * startup state has to install the stub before importing the module. Vitest
 * runs in the `node` environment (no jsdom dependency), hence this shim rather
 * than a DOM.
 */
export class MemoryStorage implements Storage {
  private map = new Map<string, string>()

  get length(): number {
    return this.map.size
  }

  clear(): void {
    this.map.clear()
  }

  getItem(key: string): string | null {
    const v = this.map.get(key)
    return v === undefined ? null : v
  }

  key(index: number): string | null {
    return Array.from(this.map.keys())[index] ?? null
  }

  removeItem(key: string): void {
    this.map.delete(key)
  }

  setItem(key: string, value: string): void {
    this.map.set(key, String(value))
  }
}
