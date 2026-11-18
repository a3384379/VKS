import mermaid from "mermaid";
import {
  validateDiagramSource,
  MAX_DIAGRAM_LENGTH,
  MAX_SVG_LENGTH,
} from "../packages/markdown-runtime/src/diagram-policy.ts";

// This trusted engine is evaluated only inside an opaque, network-disabled
// frame. User Mermaid is data passed to the parser, never JavaScript.
mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  suppressErrorRendering: true,
  maxTextSize: MAX_DIAGRAM_LENGTH,
  maxEdges: 200,
  htmlLabels: false,
  theme: "neutral",
  look: "classic",
  fontFamily: "system-ui, sans-serif",
});

window.VksReaderDiagram = async (source) => {
  const problem = validateDiagramSource("mermaid", source);
  if (problem) throw new Error(problem);
  const result = await mermaid.render("vks-reader-diagram", source);
  if (typeof result.svg !== "string" || result.svg.length > MAX_SVG_LENGTH)
    throw new Error("图表输出过大");
  return result.svg;
};
