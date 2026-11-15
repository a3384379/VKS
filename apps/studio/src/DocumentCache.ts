// One cache per authenticated catalog snapshot. No persistent browser storage.
// Navigation and heading expansion share the same in-flight request and body.
export class DocumentCache<T extends { revision_id: string }> {
  readonly id = crypto.randomUUID();
  private values = new Map<string, T>();
  private pending = new Map<string, Promise<T>>();
  private controller = new AbortController();
  constructor(
    private fetch: (
      id: string,
      native: boolean,
      signal: AbortSignal,
    ) => Promise<T>,
  ) {}
  peek(id: string, native = false) {
    return this.values.get(`${native}:${id}`);
  }
  load(id: string, native = false): Promise<T> {
    const key = `${native}:${id}`,
      cached = this.peek(id, native);
    if (cached) return Promise.resolve(cached);
    const existing = this.pending.get(key);
    if (existing) return existing;
    const request = this.fetch(id, native, this.controller.signal)
      .then((value) => {
        if (this.controller.signal.aborted)
          throw new DOMException("Aborted", "AbortError");
        if (value.revision_id !== id)
          throw new Error("文档修订不匹配，请刷新后重试");
        this.values.set(key, value);
        if (this.values.size > 64)
          this.values.delete(this.values.keys().next().value!);
        return value;
      })
      .finally(() => this.pending.delete(key));
    this.pending.set(key, request);
    return request;
  }
  dispose() {
    this.controller.abort();
    this.values.clear();
    this.pending.clear();
  }
}
