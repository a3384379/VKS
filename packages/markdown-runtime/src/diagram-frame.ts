import {
  MAX_DIAGRAM_LENGTH,
  MAX_SVG_LENGTH,
  validateDiagramSource,
} from "./diagram-policy";

// Sites bundles one engine at a time. Native builds keep both lazy imports.
declare const __VKS_DIAGRAM_ENGINE__: "mermaid" | "plantuml";
const hostOrigin = new URL(import.meta.url).origin;
let accepted = false;
window.addEventListener("message", async (event: MessageEvent) => {
  // The caller can be the owner page while the frame lives in its detached
  // view. The opaque sandbox cannot inspect parent.opener; bind requests to
  // the app origin and this frame's unguessable fragment instead.
  if (accepted || !event.source || event.origin !== hostOrigin) return;
  const data = event.data;
  if (
    !data ||
    data.type !== "vks-diagram-render" ||
    typeof data.id !== "string" ||
    data.id !== window.location.hash.slice(1) ||
    typeof data.source !== "string" ||
    data.source.length > MAX_DIAGRAM_LENGTH ||
    !["mermaid", "plantuml"].includes(data.kind)
  )
    return;
  accepted = true;
  const respond = (result: { svg: string } | { error: string }) => {
    (event.source as Window).postMessage(
      { type: "vks-diagram-result", id: data.id, ...result },
      event.origin,
    );
  };
  try {
    const problem = validateDiagramSource(data.kind, data.source);
    if (problem) throw new Error(problem);
    let svg: string;
    if (
      (typeof __VKS_DIAGRAM_ENGINE__ === "undefined" || __VKS_DIAGRAM_ENGINE__ === "mermaid") &&
      data.kind === "mermaid"
    ) {
      const { default: mermaid } = await import("mermaid");
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        suppressErrorRendering: true,
        maxTextSize: MAX_DIAGRAM_LENGTH,
        maxEdges: 200,
        htmlLabels: false,
        look: "classic",
        theme: "neutral",
        fontFamily: "system-ui, sans-serif",
      });
      const result = await mermaid.render("vks-diagram", data.source);
      svg = result.svg;
    } else if (typeof __VKS_DIAGRAM_ENGINE__ === "undefined" || __VKS_DIAGRAM_ENGINE__ === "plantuml") {
      // The bundled native JS engine uses Smetana, without Java/Graphviz or a
      // hosted PlantUML server. No file or standard-library loader is exposed.
      const runtime = globalThis as typeof globalThis & {
        PLANTUML_STDLIB_LOADER?: (
          _name: string,
          _ok: () => void,
          error: (message: string) => void,
        ) => boolean;
        PLANTUML_FILE_LOADER?: (
          _name: string,
          _from: string,
          _ok: () => void,
          error: (message: string) => void,
        ) => boolean;
      };
      runtime.PLANTUML_STDLIB_LOADER = (_name, _ok, error) => {
        error("外部图表库不可用");
        return true;
      };
      runtime.PLANTUML_FILE_LOADER = (_name, _from, _ok, error) => {
        error("文件引用不可用");
        return true;
      };
      const { renderToString } = await import("@plantuml/core");
      let source = data.source.trim();
      if (!/^@start\w+/m.test(source))
        source = "@startuml\n" + source + "\n@enduml";
      svg = await new Promise<string>((resolve, reject) => {
        renderToString(
          source.split(/\r\n?|\n/),
          resolve,
          () => reject(new Error("PlantUML 语法无法解析")),
          { maxSvgSize: 4096 },
        );
      });
      // This pinned engine can return its syntax-error card via onSuccess.
      // Recognize that card rather than presenting it as a business diagram.
      const text = Array.from(
        new DOMParser()
          .parseFromString(svg, "image/svg+xml")
          .querySelectorAll("text"),
      ).map((node) => node.textContent || "");
      if (
        text[0]?.startsWith("PlantUML version ") &&
        text.some((line) =>
          /\[From (?:textarea|string) \(line \d+\)/.test(line),
        ) &&
        text.some((line) => /Syntax Error\?/.test(line))
      )
        throw new Error("PlantUML 语法无法解析");
    } else {
      throw new Error("图表引擎不可用");
    }
    if (typeof svg !== "string" || svg.length > MAX_SVG_LENGTH)
      throw new Error("图表输出过大");
    respond({ svg });
  } catch {
    respond({ error: "图表未能渲染，请检查语法，或展开源码查看" });
  }
});
window.parent.postMessage({ type: "vks-diagram-ready" }, hostOrigin);
