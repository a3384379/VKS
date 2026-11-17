type AstNode = {
  type: string;
  lang?: string | null;
  value?: string;
  properties?: { className?: unknown };
  children?: AstNode[];
};

export function remarkMathAliases() {
  return function walk(node: AstNode) {
    if (
      node.type === "code" &&
      ["latex", "tex"].includes(node.lang?.toLowerCase() || "")
    )
      node.lang = "math";
    node.children?.forEach(walk);
  };
}

/** Limit expansion work before passing a formula to KaTeX. */
export function rehypeMathQuota() {
  function text(node: AstNode): string {
    return node.type === "text"
      ? node.value || ""
      : (node.children || []).map(text).join("");
  }
  return function walk(node: AstNode) {
    const classes = node.properties?.className;
    if (
      Array.isArray(classes) &&
      classes.some((value) =>
        ["language-math", "math-inline", "math-display"].includes(
          String(value),
        ),
      ) &&
      text(node).length > 8000
    ) {
      node.properties!.className = ["math-source-fallback"];
      node.children = [
        { type: "text", value: "公式过大，已显示源码：\n" + text(node) },
      ];
      return;
    }
    node.children?.forEach(walk);
  };
}
