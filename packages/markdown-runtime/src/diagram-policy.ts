export type DiagramKind = "mermaid" | "plantuml";
export const MAX_DIAGRAM_LENGTH = 24000;
export const MAX_SVG_LENGTH = 2 * 1024 * 1024;

export function diagramKind(language: unknown): DiagramKind | null {
  if (typeof language !== "string") return null;
  const name = language.toLowerCase();
  return name === "mermaid"
    ? "mermaid"
    : ["plantuml", "puml", "uml"].includes(name)
      ? "plantuml"
      : null;
}

export function validateDiagramSource(
  kind: DiagramKind,
  source: string,
): string | null {
  if (!source.trim()) return "图表内容为空";
  if (
    source.length > MAX_DIAGRAM_LENGTH ||
    source.split(/\r\n?|\n/).length > 500
  )
    return "图表过大，请拆分后查看";
  if (
    /<\s*(?:img|image|script|iframe|object|embed|svg|style|link)\b/i.test(
      source,
    )
  )
    return "图表中的嵌入资源不可用，请使用纯文本节点";
  if (
    kind === "mermaid" &&
    (/%%\s*\{/.test(source) ||
      /^\s*---/.test(source) ||
      /\b(?:img|image)\s*:/i.test(source))
  ) {
    return "图表中的自定义配置或图片不可用，请使用标准图表语法";
  }
  if (
    kind === "plantuml" &&
    (/(?:^|\n)\s*!/m.test(source) || /%[a-z_]\w*\s*\(/i.test(source))
  ) {
    return "PlantUML 的外部引用、预处理指令和函数不可用；请将图表内容写在当前代码块内";
  }
  return null;
}
