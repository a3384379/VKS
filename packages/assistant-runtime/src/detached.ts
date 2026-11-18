/** A single browser window; requests and credentials stay with the owning page. */
export class DetachedAssistantWindow {
  window: Window | null = null;
  prototypeMount: HTMLElement | null = null;
  documentMount: HTMLElement | null = null;
  private returning = new Set<() => void>();
  private watch = 0;
  private abandoned = false;
  private opening: Promise<boolean> | null = null;
  private epoch = 0;
  private disposed = false;
  private ownerLeaving = () => this.disconnect();
  constructor(
    private key: string | (() => string),
    private changed: (open: boolean) => void,
    private failed: (message: string) => void,
  ) {
    window.addEventListener("pagehide", this.ownerLeaving);
  }
  beforeReturn(callback: () => void) {
    this.returning.add(callback);
    return () => {
      this.returning.delete(callback);
    };
  }
  private get storageKey() {
    return typeof this.key === "function" ? this.key() : this.key;
  }
  open(): Promise<boolean> {
    if (this.disposed || this.abandoned) return Promise.resolve(false);
    if (this.window && !this.window.closed) {
      this.window.focus();
      return Promise.resolve(true);
    }
    if (this.opening) return this.opening;
    const task = this.openWindow(this.epoch);
    this.opening = task;
    void task.then(
      () => {
        if (this.opening === task) this.opening = null;
      },
      () => {
        if (this.opening === task) this.opening = null;
      },
    );
    return task;
  }
  private async openWindow(epoch: number) {
    let box = {
      width: 580,
      height: 800,
      left: screenX + 80,
      top: screenY + 60,
    };
    try {
      const saved = JSON.parse(localStorage.getItem(this.storageKey) || "null");
      if (saved && Object.values(saved).every(Number.isFinite))
        box = { ...box, ...saved };
    } catch {
      /* optional layout */
    }
    const width = Math.max(360, Math.round(box.width));
    const height = Math.max(320, Math.round(box.height));
    const pip = (
      window as Window & {
        documentPictureInPicture?: {
          requestWindow(options: {
            width: number;
            height: number;
          }): Promise<Window>;
        };
      }
    ).documentPictureInPicture;
    let popup: Window | null = null;
    let compact = false;
    if (pip?.requestWindow) {
      try {
        popup = await pip.requestWindow({ width, height });
        compact = true;
      } catch {
        // Browser/OS policy can deny PiP even when the interface is present.
      }
    }
    if (epoch !== this.epoch || this.disposed || this.abandoned) {
      popup?.close();
      return false;
    }
    if (!popup)
      popup = window.open(
        "about:blank",
        "vks-assistant-" + crypto.randomUUID(),
        `popup,width=${width},height=${height},left=${Math.round(box.left)},top=${Math.round(box.top)}`,
      );
    if (!popup) {
      this.failed("浏览器未允许打开独立窗口，请允许本页弹窗后重试。");
      return false;
    }
    if (popup.closed) return false;
    this.window = popup;
    // The owning page continues sending request results after the view moves.
    (popup as Window & { VKSAssistantOwner?: Window }).VKSAssistantOwner =
      window;
    const doc = popup.document;
    doc.open();
    doc.write(
      '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="connect-src \'none\';object-src \'none\';base-uri \'none\'"><title>AI 助手 · 独立窗口</title></head><body></body></html>',
    );
    doc.close();
    doc.documentElement.dataset.vksWindowMode = compact
      ? "compact"
      : "standard";
    for (const sheet of document.querySelectorAll(
      'style,link[rel="stylesheet"]',
    )) {
      // A new about:blank window can fail to fetch crossOrigin stylesheets.
      // Reuse the already loaded, trusted app CSS instead of racing a refetch.
      if (sheet instanceof HTMLLinkElement && sheet.sheet) {
        try {
          const copy = doc.createElement("style");
          copy.dataset.vksStylesheet = sheet.href;
          copy.textContent = Array.from(
            sheet.sheet.cssRules,
            (rule) => rule.cssText,
          )
            .join("\n")
            .replace(
              /url\(\s*(["']?)([^)"']+)\1\s*\)/gi,
              (original, _quote, path) => {
                if (/^(data:|blob:|#)/i.test(path.trim())) return original;
                return `url(${JSON.stringify(new URL(path.trim(), sheet.href).href)})`;
              },
            );
          doc.head.append(copy);
          continue;
        } catch {
          // External sheets without readable CSS rules retain their normal link.
        }
      }
      const clone = sheet.cloneNode(true) as HTMLElement;
      if (sheet instanceof HTMLLinkElement)
        (clone as HTMLLinkElement).href = sheet.href;
      doc.head.append(clone);
    }
    const style = doc.createElement("style");
    style.textContent = `html,body{margin:0;width:100%;height:100%;overflow:hidden}body{font-family:system-ui;background:white;color:#233d32}.vks-detached-bar{height:40px;box-sizing:border-box;display:flex;align-items:center;gap:12px;padding:5px 12px;border-bottom:1px solid #dce6df;background:#f5f9f6;font-size:12px}.vks-detached-bar button{margin-left:auto;padding:4px 10px;border:1px solid #ccdcd2;border-radius:6px;background:white;color:#285641;cursor:pointer}.vks-detached-status{padding:10px;background:#fff4df;color:#715326;font-size:13px}.vks-detached-mount{position:absolute;inset:40px 0 0;min-width:0;overflow:hidden}.vks-detached-mount[hidden]{display:none}.vks-detached-mount>.knowledge-window,.vks-detached-mount>.assistant{position:absolute!important;inset:0!important;width:100%!important;height:100%!important;max-width:none!important;max-height:none!important;border:0!important;border-radius:0!important;box-shadow:none!important;margin:0!important}.vks-detached-mount>iframe{display:block;border:0;width:100%;height:100%}.knowledge-resize,.assistant-resize,.document-ai-resize,.document-ai-drag,.assistant-detach-open{display:none!important}.vks-detached-mount .knowledge-header,.vks-detached-mount .assistant-head{cursor:default}.vks-detached-disconnected .vks-detached-mount{top:110px}`;
    doc.head.append(style);
    const toolbar = doc.createElement("div");
    toolbar.className = "vks-detached-bar";
    const title = doc.createElement("strong");
    title.textContent = compact ? "AI 助手 · 置顶小窗" : "AI 助手 · 独立窗口";
    const back = doc.createElement("button");
    back.textContent = "收回页面";
    back.onclick = () => this.attach();
    toolbar.append(title, back);
    doc.body.append(toolbar);
    this.prototypeMount = doc.createElement("div");
    this.documentMount = doc.createElement("div");
    for (const mount of [this.prototypeMount, this.documentMount]) {
      mount.className = "vks-detached-mount studio";
      doc.body.append(mount);
    }
    let previousGeometry = "";
    const remember = () => {
      if (popup.closed) return;
      try {
        const geometry = JSON.stringify({
          width: popup.innerWidth,
          height: popup.innerHeight,
          left: popup.screenX,
          top: popup.screenY,
        });
        if (geometry === previousGeometry) return;
        previousGeometry = geometry;
        localStorage.setItem(this.storageKey, geometry);
      } catch {
        /* optional */
      }
    };
    popup.addEventListener("resize", remember);
    remember();
    const closed = () => {
      if (this.window !== popup || this.abandoned) return;
      clearInterval(this.watch);
      remember();
      this.returning.forEach((callback) => callback());
      this.window = null;
      this.prototypeMount = this.documentMount = null;
      this.changed(false);
    };
    popup.addEventListener("beforeunload", closed);
    popup.addEventListener("pagehide", closed);
    this.watch = window.setInterval(() => {
      if (popup.closed) closed();
      else remember();
    }, 250);
    this.changed(true);
    return true;
  }
  show(documentAssistant: boolean) {
    if (!this.window || this.window.closed) return;
    if (this.documentMount) this.documentMount.hidden = !documentAssistant;
    if (this.prototypeMount) this.prototypeMount.hidden = documentAssistant;
  }
  attach() {
    this.epoch++;
    const popup = this.window;
    if (!popup) return;
    clearInterval(this.watch);
    this.returning.forEach((callback) => callback());
    try {
      localStorage.setItem(
        this.storageKey,
        JSON.stringify({
          width: popup.innerWidth,
          height: popup.innerHeight,
          left: popup.screenX,
          top: popup.screenY,
        }),
      );
    } catch {
      /* optional */
    }
    this.window = null;
    this.prototypeMount = this.documentMount = null;
    this.changed(false);
    popup.close();
    window.focus();
  }
  disconnect() {
    if (this.abandoned) return;
    this.epoch++;
    this.abandoned = true;
    const popup = this.window;
    if (!popup || popup.closed) return;
    clearInterval(this.watch);
    const doc = popup.document;
    doc.body.classList.add("vks-detached-disconnected");
    const note = doc.createElement("div");
    note.className = "vks-detached-status";
    note.textContent =
      "主页面已关闭或刷新。已显示内容保留，未完成回答不作为完整结果。";
    const link = doc.createElement("a");
    link.textContent = "重新打开主页面";
    link.href = location.href;
    link.target = "_blank";
    note.append(" ", link);
    doc.body.prepend(note);
    doc
      .querySelectorAll("button,input,textarea,select")
      .forEach((node) => ((node as HTMLButtonElement).disabled = true));
  }
  dispose() {
    this.disposed = true;
    window.removeEventListener("pagehide", this.ownerLeaving);
    this.attach();
  }
}

type Payload = Record<string, any>;
type ViewSnapshot = {
  pending?: { id: string; partial?: string };
  [key: string]: any;
};
type ViewWindow = Window & {
  VKSAssistantView?: { snapshot: () => ViewSnapshot; disconnect: () => void };
};
type ViewBridge = {
  message: (sender: Window, type: string, payload: Payload) => void;
  snapshot: (
    sender: Window,
    snapshot: ViewSnapshot | null,
    draft?: string,
  ) => void;
};
type BridgeWindow = Window & { VKSAssistantBridges?: Map<string, ViewBridge> };

/** Recreate the view, retaining its native history and the one in-flight request. */
export class DocumentAssistantFrame {
  frame!: HTMLIFrameElement;
  private ready = false;
  private target: HTMLElement;
  private snapshot: ViewSnapshot | null = null;
  private lastSnapshot: ViewSnapshot | null = null;
  private context: Payload = {};
  private selection: Payload | null = null;
  private delivery = new Map<
    string,
    { text: string; terminal?: { type: string; payload: Payload } }
  >();
  private listening = new Set<Window>();
  private detached = false;
  private alive = true;
  constructor(
    private source: string,
    private channel: string,
    host: HTMLElement,
    private received: (type: string, payload: Payload) => void,
  ) {
    this.target = host;
    const owner = window as BridgeWindow;
    owner.VKSAssistantBridges ||= new Map();
    owner.VKSAssistantBridges.set(channel, {
      message: (sender, type, payload) => {
        if (this.alive && sender === this.frame.contentWindow)
          this.receive(type, payload);
      },
      snapshot: (sender, snapshot, draft) => {
        if (!this.alive || sender !== this.frame.contentWindow) return;
        if (snapshot && (this.ready || !this.lastSnapshot))
          this.lastSnapshot = snapshot;
        if (draft !== undefined && this.lastSnapshot)
          this.lastSnapshot.draft = draft;
      },
    });
    this.create(host);
  }
  private listen = (event: MessageEvent) => {
    if (
      !this.alive ||
      event.source !== this.frame.contentWindow ||
      event.data?.vks_document_ai !== this.channel
    )
      return;
    const { type, payload = {} } = event.data;
    this.receive(type, payload);
  };
  private receive(type: string, payload: Payload = {}) {
    if (type === "ready") {
      this.frame.dataset.vksViewState = "restoring";
      if (this.target !== this.frame.parentElement) {
        this.moveTo(this.target);
        return;
      }
      if (this.snapshot) this.send("hydrate", this.snapshot);
      else this.connected();
      return;
    }
    if (type === "hydrated") {
      this.connected();
      return;
    }
    if (!this.ready) return;
    if (type === "selection-cleared") {
      this.selection = null;
      return;
    }
    if (type === "settled") {
      this.delivery.delete(payload.id);
      return;
    }
    if (type === "ask") this.delivery.set(payload.id, { text: "" });
    this.received(type, payload);
  }
  private create(host: HTMLElement) {
    const owner = host.ownerDocument.defaultView!;
    if (!this.listening.has(owner)) {
      owner.addEventListener("message", this.listen);
      this.listening.add(owner);
    }
    const frame = host.ownerDocument.createElement("iframe");
    frame.title = "知识手册 AI 助手";
    frame.setAttribute(
      "sandbox",
      "allow-scripts allow-same-origin allow-downloads",
    );
    frame.style.cssText = "display:block;border:0;width:100%;height:100%";
    this.frame = frame;
    frame.dataset.vksViewState = "loading";
    this.ready = false;
    frame.inert = true;
    host.append(frame);
    frame.srcdoc = this.source;
  }
  private connected() {
    this.frame.dataset.vksViewState = "ready";
    this.ready = true;
    this.frame.inert = false;
    this.send("context", { ...this.context, detached: this.detached });
    if (this.selection) {
      this.send("selection", this.selection);
      this.selection = { text: this.selection.text };
    }
    const pending = this.snapshot?.pending;
    if (pending) {
      const latest = this.delivery.get(pending.id);
      this.send("resume", {
        id: pending.id,
        text: latest?.text ?? pending.partial ?? "",
      });
      if (latest?.terminal)
        this.send(latest.terminal.type, latest.terminal.payload);
    }
    this.snapshot = null;
    this.received("ready", {});
  }
  private send(type: string, payload: Payload) {
    this.frame.contentWindow?.postMessage(
      { vks_document_ai: this.channel, type, payload },
      "*",
    );
  }
  post(type: string, payload: Payload = {}) {
    if (type === "context") this.context = payload;
    if (type === "selection") this.selection = payload;
    if (["delta", "complete", "error"].includes(type) && payload.id) {
      const record = this.delivery.get(payload.id) || { text: "" };
      if (type === "delta") record.text += String(payload.text || "");
      else record.terminal = { type, payload };
      this.delivery.set(payload.id, record);
      while (this.delivery.size > 10)
        this.delivery.delete(this.delivery.keys().next().value!);
    }
    if (this.ready && this.alive)
      this.send(
        type,
        type === "context" ? { ...payload, detached: this.detached } : payload,
      );
    if (type === "selection" && this.ready)
      this.selection = { text: payload.text };
  }
  moveTo(host: HTMLElement, detached = false) {
    this.detached = detached;
    this.target = host;
    if (this.frame.parentElement === host) {
      this.post("context", this.context);
      return;
    }
    let api: ViewWindow["VKSAssistantView"];
    try {
      api = (this.frame.contentWindow as ViewWindow | null)?.VKSAssistantView;
    } catch {
      /* Navigated/closed popup. */
    }
    const owner = this.frame.ownerDocument.defaultView;
    if (!api && this.frame.isConnected && owner && !owner.closed) return;
    this.snapshot = api?.snapshot() || this.lastSnapshot;
    this.lastSnapshot = this.snapshot;
    this.ready = false;
    this.frame.remove();
    this.create(host);
  }
  disconnect() {
    if (!this.alive) return;
    // A pagehide postMessage can be dropped while the owning page unloads.
    try {
      const view = (this.frame.contentWindow as ViewWindow | null)
        ?.VKSAssistantView;
      if (view) view.disconnect();
      else this.send("disconnected", {});
    } catch {
      this.send("disconnected", {});
    }
    this.alive = false;
  }
  dispose() {
    this.alive = false;
    this.listening.forEach((owner) =>
      owner.removeEventListener("message", this.listen),
    );
    this.frame.remove();
    this.delivery.clear();
    (window as BridgeWindow).VKSAssistantBridges?.delete(this.channel);
  }
}
