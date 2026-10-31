/** Native history is authoritative until its render has committed. */
export class HashRouteSync {
  private known: string;
  private epoch = 0;
  private pending: { epoch: number; mode: "push" | "replace" } | null = null;
  constructor(hash: string) {
    this.known = hash;
  }
  private begin(mode: "push" | "replace") {
    this.pending = { epoch: ++this.epoch, mode };
    return this.epoch;
  }
  user() {
    return this.begin("push");
  }
  native(hash: string) {
    if (hash === this.known) return null; // popstate + hashchange is one transition.
    this.known = hash;
    return this.begin("replace");
  }
  restore(hash: string) {
    this.known = hash;
    this.pending = null;
  }
  commit(renderEpoch: number, actualHash: string, nextHash: string) {
    // A native URL change may precede its event, and passive effects may belong
    // to an older render. Neither may overwrite the new location.
    if (actualHash !== this.known || renderEpoch !== this.epoch) return null;
    const mode = this.pending?.mode ?? "replace";
    this.pending = null;
    this.known = nextHash;
    return actualHash === nextHash ? "none" : mode;
  }
}
