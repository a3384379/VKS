import DOMPurify from "dompurify";
import { MAX_SVG_LENGTH, type DiagramKind } from "./diagram-policy";

function safeCss(css: string): boolean {
  const localReferences = css.replace(
    /url\(\s*["']?#[a-z_][\w:.-]*["']?\s*\)/gi,
    "",
  );
  return !/url\s*\(|@import|@font-face|expression\s*\(|behavior\s*:|\\/i.test(
    localReferences.replace(/\/\*[\s\S]*?\*\//g, ""),
  );
}

export function sanitizedSvg(source: string): string {
  if (source.length > MAX_SVG_LENGTH || /<!DOCTYPE|<!ENTITY/i.test(source))
    throw new Error("图表输出无效或过大");
  const cleaned = DOMPurify.sanitize(source, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: [
      "script",
      "foreignObject",
      "image",
      "use",
      "a",
      "animate",
      "animateMotion",
      "set",
      "iframe",
      "object",
      "embed",
    ],
    FORBID_ATTR: ["href", "xlink:href", "src"],
  });
  const doc = new DOMParser().parseFromString(cleaned, "image/svg+xml");
  const root = doc.documentElement;
  if (
    doc.querySelector("parsererror") ||
    root.localName !== "svg" ||
    root.namespaceURI !== "http://www.w3.org/2000/svg"
  )
    throw new Error("图表格式无效");
  const box = root.getAttribute("viewBox")?.trim().split(/[ ,]+/).map(Number);
  if (
    box &&
    (box.length !== 4 ||
      box.some((value) => !Number.isFinite(value)) ||
      box[2] <= 0 ||
      box[3] <= 0 ||
      box[2] > 8192 ||
      box[3] > 8192)
  )
    throw new Error("图表尺寸超限");
  if (box) {
    root.setAttribute("width", String(box[2]));
    root.setAttribute("height", String(box[3]));
    root.setAttribute(
      "style",
      (root.getAttribute("style") || "").replace(/max-width\s*:[^;]+;?/gi, ""),
    );
  }
  for (const node of [root, ...Array.from(root.querySelectorAll("*"))]) {
    if (node.localName === "style" && !safeCss(node.textContent || ""))
      throw new Error("图表包含外部样式资源");
    for (const attribute of Array.from(node.attributes)) {
      if (/^on/i.test(attribute.name)) node.removeAttribute(attribute.name);
      if (!safeCss(attribute.value)) throw new Error("图表包含外部资源");
    }
  }
  root.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  return new XMLSerializer().serializeToString(root);
}

let active = 0;
const queue: Array<() => void> = [];
const hostedFrames = new Map<DiagramKind, Promise<string>>();

function hostedDiagramFrame(kind: DiagramKind) {
  const asset = document.querySelector<HTMLMetaElement>(`meta[name="vks-diagram-${kind}"]`)?.content;
  if (!asset) return undefined;
  const cached = hostedFrames.get(kind);
  if (cached) return cached;
  // The authenticated parent retrieves only the engine. The opaque child
  // receives the source over postMessage and performs no remote requests.
  const pending = fetch(asset, { credentials: "same-origin", redirect: "error", signal: AbortSignal.timeout(15000) })
    .then(async (response) => {
      if (!response.ok) throw new Error("图表引擎未能加载");
      const code = (await response.text()).replace(/<\/script/gi, "<\\/script");
      const html = `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; base-uri 'none'; form-action 'none'"><script>${code}</script>`;
      return URL.createObjectURL(new Blob([html], { type: "text/html" }));
    })
    .catch((error) => {
      hostedFrames.delete(kind);
      throw error;
    });
  hostedFrames.set(kind, pending);
  return pending;
}

function drain() {
  while (active < 2 && queue.length) queue.shift()?.();
}
function release() {
  active--;
  drain();
}

/** No source is sent to a server. Opaque-origin frames cannot read app state. */
export function renderDiagram(
  kind: DiagramKind,
  source: string,
  signal: AbortSignal,
  hostDocument: Document = document,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const abortQueued = () => {
      const index = queue.indexOf(start);
      if (index >= 0) queue.splice(index, 1);
      reject(new Error("已取消"));
    };
    const start = () => {
      signal.removeEventListener("abort", abortQueued);
      if (signal.aborted) {
        reject(new Error("已取消"));
        return;
      }
      active++;
      const host = hostDocument.defaultView || window;
      const receivers = new Set([window, host]);
      const frame = hostDocument.createElement("iframe");
      frame.sandbox.add("allow-scripts");
      frame.setAttribute("aria-hidden", "true");
      frame.title = "本地图表渲染器";
      frame.tabIndex = -1;
      frame.className = "diagram-render-frame";
      const id = crypto.randomUUID();
      const address = new URL(
        "diagram-frame.html",
        window.location.origin + import.meta.env.BASE_URL,
      );
      address.hash = id;
      let finished = false;
      const finish = (error?: Error, svg?: string) => {
        if (finished) return;
        finished = true;
        host.clearTimeout(timer);
        receivers.forEach((receiver) =>
          receiver.removeEventListener("message", receive),
        );
        signal.removeEventListener("abort", abort);
        frame.remove();
        release();
        if (error) reject(error);
        else resolve(svg!);
      };
      const abort = () => finish(new Error("已取消"));
      const receive = (event: MessageEvent) => {
        if (event.source !== frame.contentWindow || event.origin !== "null")
          return;
        const data = event.data;
        if (!data || typeof data !== "object") return;
        if (data.type === "vks-diagram-ready") {
          frame.contentWindow?.postMessage(
            { type: "vks-diagram-render", id, kind, source },
            "*",
          );
        } else if (data.type === "vks-diagram-result" && data.id === id) {
          try {
            if (typeof data.svg !== "string")
              throw new Error("图表未能渲染，请检查语法，或展开源码查看");
            finish(undefined, sanitizedSvg(data.svg));
          } catch {
            finish(new Error("图表未能渲染，请检查语法，或展开源码查看"));
          }
        }
      };
      const timer = host.setTimeout(
        () => finish(new Error("图表渲染超时，可展开源码查看")),
        15000,
      );
      receivers.forEach((receiver) =>
        receiver.addEventListener("message", receive),
      );
      signal.addEventListener("abort", abort, { once: true });
      const mount = (url: string) => {
        if (finished) return;
        frame.src = url;
        hostDocument.body.appendChild(frame);
      };
      const hosted = hostedDiagramFrame(kind);
      if (hosted) {
        void hosted.then((url) => mount(url + "#" + id)).catch(() => {
          finish(new Error("图表引擎未能加载"));
        });
      } else {
        mount(address.href);
      }
    };
    if (active < 2) start();
    else if (queue.length < 32) {
      queue.push(start);
      signal.addEventListener("abort", abortQueued, { once: true });
    } else reject(new Error("图表较多，请稍后重新渲染"));
  });
}
